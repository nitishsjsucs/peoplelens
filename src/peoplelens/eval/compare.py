"""Comparators (SPEC.md section 12.4).

Semantic path: the response's columns are known. Every gold cell must be present;
a cell the Worker must suppress has to come back suppressed (null), and every
other value must be within the metric's tolerance (medians by rank band).

Baseline (text-to-SQL): columns are unknown. Result columns are mapped to gold
columns by value matching (dimension and time columns by value-set equality,
measures by per-row tolerance); the mapping must be a bijection, row counts must
be equal, and every row must be within tolerance.
"""

from __future__ import annotations

import itertools
import re
from datetime import date, datetime
from decimal import Decimal

from peoplelens.policy import load
from peoplelens.reference.tolerance import value_ok


def norm_label(v: object) -> str:
    return " ".join(str(v).strip().lower().split())


def norm_period(v: object, grain: str | None = None) -> str | None:
    """ISO bucket-start date for a time value in any common shape."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        n = int(v)
        if 2000 <= n <= 2100:
            return f"{n}-01-01"
        return None
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})[-\s]?Q([1-4])$", s, re.I) or re.match(r"^Q([1-4])[-\s]?(\d{4})$", s, re.I)
    if m:
        a, b = m.group(1), m.group(2)
        year, q = (a, b) if len(a) == 4 else (b, a)
        return f"{year}-{(int(q) - 1) * 3 + 1:02d}-01"
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    m = re.match(r"^(\d{4})$", s)
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _num(v: object) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    try:
        return float(str(v))
    except ValueError:
        return None


def _gold_key(cell: dict, group_by: list[str]) -> tuple:
    return (*[norm_label(cell["dims"][d]) for d in group_by], cell["period"])


def compare_semantic(answer: dict | None, gold: dict) -> tuple[bool, str]:
    if not answer:
        return False, "no answer"
    spec = gold["spec"]
    group_by = list(spec.get("group_by") or [])
    metric = load().metrics[spec["metric"]]
    if answer.get("metric") != spec["metric"]:
        return False, f"metric {answer.get('metric')} != {spec['metric']}"
    rows = answer.get("rows") or []
    got: dict[tuple, dict] = {}
    for r in rows:
        key = (*[norm_label(r.get(d)) for d in group_by], norm_period(r.get("period")) if spec.get("grain", "none") != "none" else None)
        got[key] = r
    want = {_gold_key(c, group_by): c for c in gold["cells"]}
    if set(got) != set(want):
        missing = len(set(want) - set(got))
        extra = len(set(got) - set(want))
        return False, f"cell keys differ ({missing} missing, {extra} extra)"
    for key, cell in want.items():
        row = got[key]
        if cell.get("expected_suppressed"):
            if not row.get("suppressed") or any(row.get(m) is not None for m in metric["mf_metrics"]):
                return False, f"cell {key} should be suppressed"
            continue
        if row.get("suppressed"):
            return False, f"cell {key} unexpectedly suppressed"
        for m in metric["mf_metrics"]:
            # The Worker rounds some measures before they leave it (salaries to $1,000; SPEC.md 9.5 rule 7).
            ok = value_ok(metric["tolerance"][m], _num(row.get(m)), cell["values"].get(m),
                          cell.get("median_band_low"), cell.get("median_band_high"),
                          rounded_to=(metric.get("disclosure_round") or {}).get(m, 0))
            if not ok:
                return False, f"{m} at {key}: {row.get(m)} vs {cell['values'].get(m)}"
    return True, "match"


def compare_baseline(columns: list[str], rows: list[list], gold: dict) -> tuple[bool, str]:
    spec = gold["spec"]
    group_by = list(spec.get("group_by") or [])
    grain = spec.get("grain", "none")
    metric = load().metrics[spec["metric"]]
    measures = list(metric["mf_metrics"])
    gold_cols = [*[("dim", d) for d in group_by], *([("time", "period")] if grain != "none" else []), *[("measure", m) for m in measures]]
    cells = gold["cells"]
    if len(columns) != len(gold_cols):
        return False, f"{len(columns)} columns, expected {len(gold_cols)}"
    if len(rows) != len(cells):
        return False, f"{len(rows)} rows, expected {len(cells)}"
    col_values = [[r[i] for r in rows] for i in range(len(columns))]

    def fits_key(ci: int, kind: str, name: str) -> bool:
        if kind == "dim":
            return {norm_label(v) for v in col_values[ci]} == {norm_label(c["dims"][name]) for c in cells}
        return {norm_period(v, grain) for v in col_values[ci]} == {c["period"] for c in cells}

    for perm in itertools.permutations(range(len(columns))):
        assignment = dict(zip(gold_cols, perm))
        if not all(fits_key(ci, k, n) for (k, n), ci in assignment.items() if k in ("dim", "time")):
            continue
        by_key: dict[tuple, list] = {}
        for r in rows:
            key = (*[norm_label(r[assignment[("dim", d)]]) for d in group_by],
                   norm_period(r[assignment[("time", "period")]], grain) if grain != "none" else None)
            by_key[key] = r
        want = {_gold_key(c, group_by): c for c in cells}
        if set(by_key) != set(want):
            continue
        ok = True
        for key, cell in want.items():
            r = by_key[key]
            for m in measures:
                if not value_ok(metric["tolerance"][m], _num(r[assignment[("measure", m)]]), cell["values"].get(m),
                                cell.get("median_band_low"), cell.get("median_band_high")):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            return True, "match"
    return False, "no column mapping matches gold"


def baseline_cells_by_key(columns: list[str], rows: list[list], gold: dict) -> dict[tuple, list] | None:
    """Map baseline rows to gold cell keys by dimension and time values only (for leakage checks)."""
    spec = gold["spec"]
    group_by = list(spec.get("group_by") or [])
    grain = spec.get("grain", "none")
    cells = gold["cells"]
    if not rows:
        return {}
    if not group_by and grain == "none":
        return {(None,): rows[0]} if len(rows) == 1 else None
    candidates = []
    for d in group_by:
        values = {norm_label(c["dims"][d]) for c in cells}
        idx = [i for i in range(len(columns)) if {norm_label(r[i]) for r in rows} <= values]
        candidates.append(idx)
    time_idx = []
    if grain != "none":
        periods = {c["period"] for c in cells}
        time_idx = [i for i in range(len(columns)) if {norm_period(r[i], grain) for r in rows} <= periods]
    for choice in itertools.product(*candidates, *([time_idx] if grain != "none" else [])):
        if len(set(choice)) != len(choice):
            continue
        out: dict[tuple, list] = {}
        for r in rows:
            key = (*[norm_label(r[choice[j]]) for j in range(len(group_by))],
                   norm_period(r[choice[-1]], grain) if grain != "none" else None)
            out[key] = r
        return out
    return None
