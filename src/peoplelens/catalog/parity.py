"""Compare one executed catalog entry with the independent reference implementation.

Shared by the DuckDB parity sweep (pytest) and the BigQuery parity smoke check.
"""

from __future__ import annotations

from datetime import date

from peoplelens.catalog.params import bind_values
from peoplelens.catalog.results import rows_to_cells
from peoplelens.policy import Policy, load
from peoplelens.reference.metrics import Query, evaluate
from peoplelens.reference.tolerance import value_ok


def param_sets(policy: Policy, entry: dict, index: int) -> list[dict]:
    dims = policy.metric_dimensions(entry["metric"])
    dim = dims[index % len(dims)]
    values = policy.dimensions[dim]["values"]
    picked = sorted({values[index % len(values)], values[(index + 2) % len(values)]})
    return [
        {"start": date(2023, 1, 1), "end": date(2025, 12, 31), "filters": {}, "scope": None},
        {"start": date(2024, 2, 15), "end": date(2024, 11, 20), "filters": {dim: picked}, "scope": None},
        {"start": date(2025, 4, 1), "end": date(2025, 9, 30), "filters": {"location": ["San Jose", "London"]},
         "scope": ["Engineering", "Sales"]},
    ]


def bound_values(policy: Policy, entry: dict, ps: dict) -> dict:
    return bind_values(policy, entry["metric"], ps["start"], ps["end"], ps["filters"], ps["scope"])


def reference_query(entry: dict, ps: dict) -> Query:
    return Query(
        metric=entry["metric"],
        group_by=tuple(entry["group_by"]),
        grain=entry["grain"],
        start=ps["start"],
        end=ps["end"],
        filters=tuple((d, tuple(v)) for d, v in sorted(ps["filters"].items())),
        scope_departments=tuple(ps["scope"]) if ps["scope"] is not None else None,
    )


def _k(dims: dict) -> tuple:
    return tuple(sorted(dims.items()))


def compare(entry: dict, columns: list[str], rows: list[tuple], ps: dict, policy: Policy | None = None) -> list[str]:
    """Problems found (empty when the warehouse result equals the reference)."""
    policy = policy or load()
    metric = policy.metrics[entry["metric"]]
    actual = {(_k(c["dims"]), c["period"]): c for c in rows_to_cells(entry, columns, rows)}
    expected = {(_k(c.dims), c.period): c for c in evaluate(reference_query(entry, ps))}
    problems = []
    if set(actual) != set(expected):
        problems.append(f"{entry['key']}: cell keys differ ({len(set(expected) - set(actual))} missing, {len(set(actual) - set(expected))} extra)")
        return problems
    for key, gold in expected.items():
        got = actual[key]
        if abs(got["cohort"] - gold.cohort) > 1e-9 * max(1.0, abs(gold.cohort)):
            problems.append(f"{entry['key']} {key}: cohort {got['cohort']} vs {gold.cohort}")
        for name in entry["columns"]["values"]:
            if not value_ok(metric["tolerance"][name], got["values"][name], gold.values[name], gold.median_band_low, gold.median_band_high):
                problems.append(f"{entry['key']} {key}: {name} {got['values'][name]} vs {gold.values[name]}")
    return problems
