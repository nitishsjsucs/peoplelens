"""Every one of the 312 DuckDB catalog entries, with 3 parameter sets each, equals
the independent Python reference implementation (SPEC.md 12.2 and 13)."""

from __future__ import annotations

import json

import duckdb
import pytest

from peoplelens.catalog.parity import bound_values, compare, param_sets
from peoplelens.paths import GENERATED_DIR
from peoplelens.policy import load

POLICY = load()
CATALOG = json.loads((GENERATED_DIR / "catalog.duckdb.json").read_text())
ENTRIES = sorted(CATALOG["entries"].values(), key=lambda e: e["key"])


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
    for ps in param_sets(POLICY, entry, index):
        cur = con.execute(entry["sql"], bound_values(POLICY, entry, ps))
        problems = compare(entry, [d[0] for d in cur.description], cur.fetchall(), ps, POLICY)
        assert problems == [], (ps, problems[:5])
