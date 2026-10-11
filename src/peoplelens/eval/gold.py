"""Gold results from the independent reference implementation (`pl-eval gold`).

A gold spec is {metric, group_by, grain, start, end, filters: {dim: [values]},
scope: [departments] | absent}. Gold cells carry the cohort (synthetic data,
eval-only file), median rank bands, and the suppression the Worker must apply
(primary, complement rule, complementary, hidden sum).
"""

from __future__ import annotations

import json
from datetime import date
from itertools import combinations
from pathlib import Path

from peoplelens.eval.suppression import cell_key, complement_keys, suppress
from peoplelens.paths import EVAL_DIR
from peoplelens.policy import load
from peoplelens.reference.metrics import Query, evaluate

GOLD_PATH = EVAL_DIR / "gold.json"


def spec_query(spec: dict) -> Query:
    filters = tuple(sorted((d, tuple(sorted(v))) for d, v in (spec.get("filters") or {}).items()))
    scope = spec.get("scope")
    return Query(
        metric=spec["metric"],
        group_by=tuple(spec.get("group_by") or ()),
        grain=spec.get("grain", "none"),
        start=date.fromisoformat(spec["start"]),
        end=date.fromisoformat(spec["end"]),
        filters=filters,
        scope_departments=tuple(sorted(scope)) if scope else None,
    )


def restricted_dims(spec: dict) -> list[str]:
    policy = load()
    out = set()
    for d, vals in (spec.get("filters") or {}).items():
        if len(set(vals)) < len(policy.dimensions[d]["values"]):
            out.add(d)
    scope = spec.get("scope")
    if scope and len(scope) < len(policy.dimensions["department"]["values"]):
        out.add("department")
    return sorted(out)


def widened(spec: dict, dims: tuple[str, ...]) -> dict:
    out = dict(spec)
    out["filters"] = {d: v for d, v in (spec.get("filters") or {}).items() if d not in dims}
    if "department" in dims:
        out.pop("scope", None)
    return out


def scope_widened(spec: dict, dims: tuple[str, ...]) -> dict:
    """`widened(spec, dims)` with the department filter also removed but the HRBP scope kept."""
    out = widened(spec, dims)
    out["filters"] = {d: v for d, v in out["filters"].items() if d != "department"}
    return out


def probe_specs(spec: dict) -> list[dict]:
    """The complement-rule probes of worker/src/guard/complement.ts: every non-empty set of
    restricted dimensions widened to all values; and, when a department filter sits inside
    an HRBP scope, the same query with that filter widened to the scope (combined with every
    set of the other restricted dimensions), because the HRBP can ask for the whole scope."""
    r = restricted_dims(spec)
    out = [widened(spec, combo) for size in range(1, len(r) + 1) for combo in combinations(r, size)]
    if spec.get("scope") and (spec.get("filters") or {}).get("department"):
        others = [d for d in r if d != "department"]
        out += [scope_widened(spec, combo) for size in range(0, len(others) + 1) for combo in combinations(others, size)]
    return out


def _cells(spec: dict) -> list[dict]:
    out = []
    for c in evaluate(spec_query(spec)):
        cell = {"dims": dict(c.dims), "period": c.period, "values": dict(c.values), "cohort": c.cohort}
        if c.median_lower is not None:
            cell.update(
                median_lower=c.median_lower,
                median_upper=c.median_upper,
                median_band_low=c.median_band_low,
                median_band_high=c.median_band_high,
            )
        out.append(cell)
    return out


def gold_for_spec(spec: dict) -> dict:
    k = load().limits["min_cohort"]
    dims = list(spec.get("group_by") or [])
    main = _cells(spec)
    restricted = restricted_dims(spec)
    probes = [_cells(p) for p in probe_specs(spec)]
    forced = complement_keys(main, probes, dims, k)
    reasons = suppress(main, dims, k, forced)
    for c in main:
        key = cell_key(c["dims"], dims, c["period"])
        c["expected_suppressed"] = key in reasons
        if key in reasons:
            c["suppression_reason"] = reasons[key]
    return {"spec": spec, "restricted": restricted, "cells": main}


def all_specs(items: list[dict]) -> dict[str, dict]:
    """Gold keys: `<item id>` for answerable and two-turn items, `<item id>#<turn>` for per-turn specs."""
    out: dict[str, dict] = {}
    for item in items:
        if item.get("gold_spec"):
            out[item["id"]] = item["gold_spec"]
        for i, spec in enumerate(item.get("gold_specs") or []):
            if spec:
                out[f"{item['id']}#{i}"] = spec
    return out


def build(items: list[dict]) -> dict:
    return {key: gold_for_spec(spec) for key, spec in sorted(all_specs(items).items())}


def dump(gold: dict) -> str:
    return json.dumps(gold, indent=1, sort_keys=True) + "\n"


def load_gold(path: Path = GOLD_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
