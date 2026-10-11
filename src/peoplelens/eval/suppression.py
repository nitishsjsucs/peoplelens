"""Python port of the Worker's suppression rules (worker/src/guard/*.ts), used to
derive expected suppression for gold and to filter answerable eval items."""

from __future__ import annotations

import json

EPS = 1e-9


def cell_key(dims: dict, order: list[str], period: str | None) -> str:
    return json.dumps([*[dims.get(d) for d in order], period])


def suppress(cells: list[dict], dims: list[str], k: int, forced: set[str] | None = None) -> dict[str, str]:
    """Return {cell key: reason} for suppressed cells. Cells: {dims, period, cohort}."""
    reason: dict[str, str] = {}
    keyed = [(cell_key(c["dims"], dims, c["period"]), c) for c in cells]
    for key, c in keyed:
        if c["cohort"] < k - EPS:
            reason[key] = "primary"
        elif forced and key in forced:
            reason[key] = "complement"
    changed = True
    while changed:
        changed = False
        for d in dims:
            parts: dict[str, list[tuple[str, dict]]] = {}
            for key, c in keyed:
                pk = json.dumps([*[c["dims"].get(x) for x in dims if x != d], c["period"]])
                parts.setdefault(pk, []).append((key, c))
            for pk in sorted(parts):
                group = parts[pk]
                if len(group) <= 1:
                    continue
                hidden = [(key, c) for key, c in group if key in reason]
                open_ = sorted([(key, c) for key, c in group if key not in reason], key=lambda kc: (kc[1]["cohort"], kc[0]))
                if not hidden or not open_:
                    continue
                if len(hidden) == 1:
                    reason[open_[0][0]] = "complementary"
                    changed = True
                    continue
                total = sum(c["cohort"] for _, c in hidden)
                if EPS < total < k - EPS:
                    reason[open_[0][0]] = "hidden_sum"
                    changed = True
    return reason


def complement_keys(main: list[dict], probes: list[list[dict]], dims: list[str], k: int) -> set[str]:
    out: set[str] = set()
    probe_maps = [{cell_key(c["dims"], dims, c["period"]): c["cohort"] for c in p} for p in probes]
    for c in main:
        key = cell_key(c["dims"], dims, c["period"])
        for m in probe_maps:
            wide = m.get(key)
            if wide is None:
                continue
            diff = wide - c["cohort"]
            if EPS < diff < k - EPS:
                out.add(key)
                break
    return out
