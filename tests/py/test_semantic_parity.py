"""Every one of the 312 DuckDB catalog entries, with 3 parameter sets each, equals
the independent Python reference implementation (SPEC.md 12.2 and 13)."""

from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from peoplelens.catalog.params import bind_values
from peoplelens.catalog.results import rows_to_cells
from peoplelens.paths import GENERATED_DIR
from peoplelens.policy import load
from peoplelens.reference.metrics import Query, evaluate
from peoplelens.reference.tolerance import value_ok

POLICY = load()
CATALOG = json.loads((GENERATED_DIR / "catalog.duckdb.json").read_text())
ENTRIES = sorted(CATALOG["entries"].values(), key=lambda e: e["key"])


def param_sets(entry: dict, index: int) -> list[dict]:
    dims = POLICY.metric_dimensions(entry["metric"])
    dim = dims[index % len(dims)]
    values = POLICY.dimensions[dim]["values"]
    picked = sorted({values[index % len(values)], values[(index + 2) % len(values)]})
    return [
        {"start": date(2023, 1, 1), "end": date(2025, 12, 31), "filters": {}, "scope": None},
        {"start": date(2024, 2, 15), "end": date(2024, 11, 20), "filters": {dim: picked}, "scope": None},
        {"start": date(2025, 4, 1), "end": date(2025, 9, 30), "filters": {"location": ["San Jose", "London"]},
         "scope": ["Engineering", "Sales"]},
    ]


@pytest.fixture(scope="module")
def con(require_warehouse):
    c = duckdb.connect(str(require_warehouse), read_only=True)
    yield c
    c.close()


def test_catalog_has_312_entries():
    assert CATALOG["header"]["entries"] == len(ENTRIES) == 312


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["key"] for e in ENTRIES])
def test_entry_matches_reference(con, entry):
    index = ENTRIES.index(entry)
    metric = POLICY.metrics[entry["metric"]]
    for ps in param_sets(entry, index):
        params = bind_values(POLICY, entry["metric"], ps["start"], ps["end"], ps["filters"], ps["scope"])
        cur = con.execute(entry["sql"], params)
        columns = [d[0] for d in cur.description]
        actual = {(_k(c["dims"]), c["period"]): c for c in rows_to_cells(entry, columns, cur.fetchall())}
        q = Query(
            metric=entry["metric"],
            group_by=tuple(entry["group_by"]),
            grain=entry["grain"],
            start=ps["start"],
            end=ps["end"],
            filters=tuple((d, tuple(v)) for d, v in sorted(ps["filters"].items())),
            scope_departments=tuple(ps["scope"]) if ps["scope"] is not None else None,
        )
        expected = {(_k(c.dims), c.period): c for c in evaluate(q)}
        assert set(actual) == set(expected), (entry["key"], ps)
        for key, gold in expected.items():
            got = actual[key]
            assert got["cohort"] == pytest.approx(gold.cohort, rel=1e-9), (entry["key"], ps, key)
            for name in entry["columns"]["values"]:
                ok = value_ok(metric["tolerance"][name], got["values"][name], gold.values[name],
                              gold.median_band_low, gold.median_band_high)
                assert ok, (entry["key"], ps, key, name, got["values"][name], gold.values[name])


def _k(dims: dict) -> tuple:
    return tuple(sorted(dims.items()))
