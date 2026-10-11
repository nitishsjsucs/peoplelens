"""Exact counts asserted on the dbt marts in the DuckDB warehouse (SPEC.md 5.3).

The dbt singular tests assert the same numbers inside dbt; this module checks
them again from Python against the spec constants, through a read-only connection.
"""

from __future__ import annotations

import duckdb
import pytest

from peoplelens.synth import spec


@pytest.fixture(scope="module")
def con(require_warehouse):
    c = duckdb.connect(str(require_warehouse), read_only=True)
    yield c
    c.close()


def _closing(con, column):
    rows = con.execute(
        f"select {column}, sum(is_active_eom) from peoplelens.semantic.sem_workforce_monthly "
        "where month_start = date '2025-12-01' group by 1"
    ).fetchall()
    return {k: int(v) for k, v in rows}


def test_closing_marginals_on_semantic_view(con):
    assert _closing(con, "department") == spec.CLOSING_BY_DEPARTMENT
    assert _closing(con, "location") == spec.CLOSING_BY_LOCATION
    assert _closing(con, "job_level") == spec.CLOSING_BY_LEVEL


def test_closing_headcount_and_fte(con):
    n, fte = con.execute(
        "select sum(is_active_eom), sum(case when is_active_eom = 1 then fte_eom else 0 end) "
        "from peoplelens.semantic.sem_workforce_monthly where month_start = date '2025-12-01'"
    ).fetchone()
    assert n == spec.CLOSING_HEADCOUNT
    assert fte == pytest.approx(spec.CLOSING_FTE, abs=1e-9)


def test_terminations_in_window(con):
    total, vol = con.execute(
        "select sum(is_termination), sum(is_voluntary_termination) from peoplelens.semantic.sem_workforce_monthly"
    ).fetchone()
    assert total == sum(spec.TERMINATIONS_BY_YEAR.values())
    assert vol == spec.VOLUNTARY_TERMINATIONS


def test_one_row_per_employee_month(con):
    dup = con.execute(
        "select count(*) - count(distinct employee_id || '|' || cast(month_start as varchar)) "
        "from peoplelens.core.fct_workforce_monthly"
    ).fetchone()[0]
    assert dup == 0
    months = con.execute("select count(*) from peoplelens.core.dim_month").fetchone()[0]
    assert months == 36


def test_recruiting_marts(con):
    assert con.execute("select count(*) from peoplelens.semantic.sem_requisitions").fetchone()[0] == spec.REQUISITIONS_FILLED
    extended, accepted = con.execute("select count(*), sum(is_accepted) from peoplelens.semantic.sem_offers").fetchone()
    assert (extended, accepted) == (spec.OFFERS, spec.OFFERS_ACCEPTED)


def test_semantic_views_carry_no_pii_or_person_ids(con):
    cols = {
        r[0]
        for r in con.execute(
            "select column_name from information_schema.columns where table_schema = 'semantic'"
        ).fetchall()
    }
    for banned in ("employee_id", "full_name", "work_email", "date_of_birth", "gender", "candidate_id", "email"):
        assert banned not in cols


def test_staging_drops_pii(con):
    cols = {
        r[0]
        for r in con.execute(
            "select column_name from information_schema.columns where table_schema = 'staging'"
        ).fetchall()
    }
    for banned in ("full_name", "work_email", "date_of_birth", "gender", "candidate_id", "email"):
        assert banned not in cols


def test_warehouse_connection_is_read_only(con):
    with pytest.raises(duckdb.Error):
        con.execute("create table core.should_fail (x int)")
