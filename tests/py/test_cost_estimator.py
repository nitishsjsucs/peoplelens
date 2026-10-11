"""Local BigQuery byte estimator, with values pinned on fixture tables."""

from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from peoplelens.catalog.params import bind_values
from peoplelens.paths import GENERATED_DIR
from peoplelens.policy import load
from peoplelens.warehouse.cost import BQ_MIN_BYTES_PER_TABLE, ByteEstimator, logical_size


def test_logical_sizes():
    assert logical_size("BIGINT") == 8
    assert logical_size("DOUBLE") == 8
    assert logical_size("DATE") == 8
    assert logical_size("TIMESTAMP") == 8
    assert logical_size("BOOLEAN") == 1
    assert logical_size("DECIMAL(18,2)") == 16
    assert logical_size("VARCHAR", 5.0) == 7.0
    with pytest.raises(ValueError):
        logical_size("BLOB")


@pytest.fixture()
def fixture_db(tmp_path):
    path = tmp_path / "fx.duckdb"
    con = duckdb.connect(str(path))
    con.execute("create schema s")
    # 1,000 rows: name is 'abcd' (4 bytes) or 'abcdefgh' (8 bytes), alternating -> average 6.
    con.execute(
        "create table s.small as select range::BIGINT as id, "
        "case when range % 2 = 0 then 'abcd' else 'abcdefgh' end as name, "
        "(range * 1.5)::DOUBLE as amount, date '2025-01-01' as d, range % 3 = 0 as flag from range(1000)"
    )
    # 1,000,000 rows of (BIGINT, DOUBLE): 16 MB of logical bytes, above the 10 MB minimum.
    con.execute("create table s.big as select range::BIGINT as id, (range * 0.5)::DOUBLE as v from range(1000000)")
    con.close()
    ro = duckdb.connect(str(path), read_only=True)
    yield ro
    ro.close()


def test_small_table_hits_the_10_mb_minimum(fixture_db):
    est = ByteEstimator(fixture_db.cursor).estimate("select name, amount from s.small where flag")
    detail = est.tables["fx.s.small"]
    # name (2 + 6) + amount 8 + flag 1 = 17 bytes x 1,000 rows
    assert detail["columns"] == ["amount", "flag", "name"]
    assert detail["raw_bytes"] == 17000
    assert detail["billed_bytes"] == BQ_MIN_BYTES_PER_TABLE
    assert est.total_bytes == BQ_MIN_BYTES_PER_TABLE


def test_large_table_bills_logical_bytes(fixture_db):
    est = ByteEstimator(fixture_db.cursor).estimate("select sum(v) from s.big where id > 10")
    detail = est.tables["fx.s.big"]
    assert detail["columns"] == ["id", "v"]
    assert detail["raw_bytes"] == 16_000_000
    assert est.total_bytes == 16_000_000


def test_each_referenced_table_is_billed_once(fixture_db):
    est = ByteEstimator(fixture_db.cursor).estimate(
        "select count(*) from s.small a join s.small b on a.id = b.id join s.big c on c.id = a.id"
    )
    assert set(est.tables) == {"fx.s.small", "fx.s.big"}
    assert est.total_bytes == BQ_MIN_BYTES_PER_TABLE + max(BQ_MIN_BYTES_PER_TABLE, est.tables["fx.s.big"]["raw_bytes"])


def test_catalog_entry_estimate_is_pinned(require_warehouse):
    policy = load()
    catalog = json.loads((GENERATED_DIR / "catalog.duckdb.json").read_text())
    entry = catalog["entries"]["compensation_summary|department|quarter"]
    con = duckdb.connect(str(require_warehouse), read_only=True)
    try:
        params = bind_values(policy, entry["metric"], date(2025, 1, 1), date(2025, 12, 31))
        est = ByteEstimator(con.cursor).estimate(entry["sql"], params)
    finally:
        con.close()
    detail = est.tables["peoplelens.core.fct_workforce_monthly"]
    assert detail["rows"] == 59977
    assert detail["columns"] == ["base_salary_eom", "department", "is_active_eom", "month_start"]
    # department: 2 + average UTF-8 length; the three other columns are 8 bytes each.
    assert detail["raw_bytes"] < BQ_MIN_BYTES_PER_TABLE
    assert est.total_bytes == BQ_MIN_BYTES_PER_TABLE
    assert list(est.tables) == ["peoplelens.core.fct_workforce_monthly"]
