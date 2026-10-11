"""Parameter contract: '__all__' sentinels instead of NULL, scope as a bound array."""

from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from peoplelens.catalog.params import ALL, bind_values, param_contract, param_names, where_filters
from peoplelens.paths import GENERATED_DIR
from peoplelens.policy import load

POLICY = load()
CATALOG = json.loads((GENERATED_DIR / "catalog.duckdb.json").read_text())


@pytest.mark.parametrize("metric", sorted(POLICY.metrics))
def test_defaults_never_bind_null(metric):
    values = bind_values(POLICY, metric, date(2025, 1, 1), date(2025, 12, 31))
    assert list(values) == param_names(POLICY, metric)
    for name, value in values.items():
        assert value is not None
        if name.startswith("f_"):
            assert value == [ALL]
    assert values["scope_departments"] == POLICY.dimensions["department"]["values"]


def test_param_types_per_dialect():
    duck = {p["name"]: p["type"] for p in param_contract(POLICY, "headcount", "duckdb")}
    bq = {p["name"]: p["type"] for p in param_contract(POLICY, "headcount", "bigquery")}
    assert duck["start_ts"] == "DATE" and bq["start_ts"] == "TIMESTAMP"
    assert duck["f_department"] == "VARCHAR[]" and bq["f_department"] == "ARRAY<STRING>"
    assert set(duck) == set(bq) == {"start_ts", "end_ts", "f_department", "f_location", "f_job_level",
                                    "f_employment_type", "f_tenure_band", "scope_departments"}
    assert set(param_names(POLICY, "time_to_fill_days")) == {
        "start_ts", "end_ts", "f_department", "f_location", "f_job_level", "scope_departments"}


def test_where_filters_use_the_metric_entity():
    assert any("offer__location" in w for w in where_filters(POLICY, "offer_acceptance_rate", "bigquery"))
    assert any("TimeDimension('metric_time', 'day')" in w for w in where_filters(POLICY, "time_to_fill_days", "duckdb"))
    assert any("TimeDimension('metric_time', 'month')" in w for w in where_filters(POLICY, "headcount", "duckdb"))


def test_sentinel_equals_explicit_full_value_list(require_warehouse):
    entry = CATALOG["entries"]["headcount|location|none"]
    con = duckdb.connect(str(require_warehouse), read_only=True)
    try:
        sentinel = bind_values(POLICY, "headcount", date(2025, 12, 1), date(2025, 12, 31))
        explicit = bind_values(
            POLICY, "headcount", date(2025, 12, 1), date(2025, 12, 31),
            filters={d: POLICY.dimensions[d]["values"] for d in POLICY.metric_dimensions("headcount")},
        )
        a = sorted(con.execute(entry["sql"], sentinel).fetchall())
        b = sorted(con.execute(entry["sql"], explicit).fetchall())
        assert a == b and len(a) == 5
        only_london = bind_values(POLICY, "headcount", date(2025, 12, 1), date(2025, 12, 31),
                                  filters={"location": ["London"]})
        rows = con.execute(entry["sql"], only_london).fetchall()
        assert [r[0] for r in rows] == ["London"]
    finally:
        con.close()
