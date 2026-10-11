"""Compiled catalogs: 312 entries per dialect, parameter contract, column maps."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import version

import pytest

from peoplelens.catalog.keys import catalog_key, shapes
from peoplelens.catalog.params import param_names
from peoplelens.paths import GENERATED_DIR
from peoplelens.policy import load

POLICY = load()
CATALOGS = {
    d: json.loads((GENERATED_DIR / f"catalog.{d}.json").read_text()) for d in ("duckdb", "bigquery")
}


def test_shape_count_is_312():
    all_shapes = shapes(POLICY)
    assert len(all_shapes) == 312
    workforce = [s for s in all_shapes if POLICY.metrics[s[0]]["entity"] == "workforce"]
    assert len(workforce) == 4 * 16 * 4
    assert len(all_shapes) - len(workforce) == 2 * 7 * 4


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_each_dialect_has_every_shape(dialect):
    cat = CATALOGS[dialect]
    assert cat["header"]["entries"] == 312
    assert cat["header"]["dialect"] == dialect
    expected = {catalog_key(m, g, gr) for m, g, gr in shapes(POLICY)}
    assert set(cat["entries"]) == expected


def test_catalog_key_format():
    assert catalog_key("turnover_rate", ["department"], "quarter") == "turnover_rate|department|quarter"
    assert catalog_key("headcount", ("location", "department"), "none") == "headcount|department,location|none"
    assert catalog_key("fte", (), "year") == "fte||year"


@pytest.mark.parametrize("dialect", ["duckdb", "bigquery"])
def test_entries_carry_cohort_metric_and_contract(dialect):
    for key, e in CATALOGS[dialect]["entries"].items():
        m = POLICY.metrics[e["metric"]]
        assert m["cohort_metric"] in e["mf_metrics"], key
        assert e["columns"]["cohort"] == m["cohort_metric"]
        assert e["columns"]["values"] == m["mf_metrics"]
        assert [p["name"] for p in e["params"]] == param_names(POLICY, e["metric"])
        assert hashlib.sha256(e["sql"].encode()).hexdigest() == e["sha256"]
        assert not any(line.strip().startswith("--") for line in e["sql"].splitlines()), key
        for name in param_names(POLICY, e["metric"]):
            marker = "@" if dialect == "bigquery" else "$"
            assert f"{marker}{name}" in e["sql"], (key, name)
        if e["grain"] == "none":
            assert e["columns"]["time"] is None
        else:
            assert e["columns"]["time"] == f"metric_time__{e['grain']}"


def test_duckdb_dialect_specifics():
    for key, e in CATALOGS["duckdb"]["entries"].items():
        sql = e["sql"]
        assert "@" not in sql and "UNNEST(" not in sql, key
        assert "list_contains($scope_departments" in sql
        assert '"peoplelens"."semantic".' in sql
        assert all(p["type"] in ("DATE", "VARCHAR[]") for p in e["params"])


def test_header_records_tool_versions():
    for cat in CATALOGS.values():
        h = cat["header"]
        assert h["metricflow"] == version("metricflow") == "0.213.0"
        assert h["dbt_metricflow"] == version("dbt-metricflow") == "0.15.0"
        assert h["dbt_core"] == version("dbt-core") == "1.12.5"
        assert len(h["manifest_sha256"]) == 64


def test_generated_dimension_values_match_policy():
    values = json.loads((GENERATED_DIR / "dimension_values.json").read_text())
    assert values == {d: POLICY.dimensions[d]["values"] for d in POLICY.dimensions}


def test_both_dialects_share_column_maps():
    for key, e in CATALOGS["duckdb"]["entries"].items():
        b = CATALOGS["bigquery"]["entries"][key]
        assert e["columns"] == b["columns"]
        assert e["mf_metrics"] == b["mf_metrics"]
