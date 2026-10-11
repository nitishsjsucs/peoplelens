"""Exact counts from SPEC.md section 5.3, asserted on the seed CSVs on disk."""

from __future__ import annotations

import pytest

from peoplelens.synth import spec
from peoplelens.synth.stats import compute, read_csv


@pytest.fixture(scope="module")
def stats():
    return compute()


def test_spec_constants_are_consistent():
    assert spec.OPENING_HEADCOUNT + sum(spec.HIRES_BY_YEAR.values()) == spec.TOTAL_EMPLOYEES
    terms = sum(spec.TERMINATIONS_BY_YEAR.values())
    assert spec.VOLUNTARY_TERMINATIONS + spec.INVOLUNTARY_TERMINATIONS == terms
    assert spec.OPENING_HEADCOUNT + sum(spec.HIRES_BY_YEAR.values()) - terms == spec.CLOSING_HEADCOUNT
    for marginal in (spec.CLOSING_BY_DEPARTMENT, spec.CLOSING_BY_LOCATION, spec.CLOSING_BY_LEVEL):
        assert sum(marginal.values()) == spec.CLOSING_HEADCOUNT
    full_time = spec.CLOSING_HEADCOUNT - spec.CLOSING_PART_TIME
    fte = full_time + 0.5 * spec.CLOSING_PART_TIME_HALF + 0.8 * spec.CLOSING_PART_TIME_FOUR_FIFTHS
    assert fte == pytest.approx(spec.CLOSING_FTE)
    assert spec.REQUISITIONS_FILLED + spec.REQUISITIONS_OPEN + spec.REQUISITIONS_CANCELLED == spec.REQUISITIONS
    assert spec.OFFERS_ACCEPTED + spec.OFFERS_DECLINED == spec.OFFERS
    assert spec.DECLINED_ON_FILLED + spec.DECLINED_ON_OPEN + spec.DECLINED_ON_CANCELLED == spec.OFFERS_DECLINED
    assert spec.JOB_HISTORY_ROWS == 2820


def test_headcount_flow(stats):
    assert stats["employees"] == spec.TOTAL_EMPLOYEES == 2400
    assert stats["opening_headcount"] == spec.OPENING_HEADCOUNT == 1500
    assert stats["hires_by_year"] == spec.HIRES_BY_YEAR
    assert stats["hires"] == 900
    assert stats["terminations_by_year"] == spec.TERMINATIONS_BY_YEAR
    assert stats["terminations"] == 620
    assert stats["voluntary"] == 450
    assert stats["involuntary"] == 170
    assert stats["closing_headcount"] == spec.CLOSING_HEADCOUNT == 1780


def test_closing_fte_and_part_time(stats):
    assert stats["closing_part_time_half"] == 80
    assert stats["closing_part_time_four_fifths"] == 80
    assert stats["closing_fte"] == spec.CLOSING_FTE == 1724.0


def test_closing_marginals(stats):
    assert stats["closing_by_department"] == spec.CLOSING_BY_DEPARTMENT
    assert stats["closing_by_department"]["Legal"] == 8
    assert stats["closing_by_location"] == spec.CLOSING_BY_LOCATION
    assert stats["closing_by_level"] == spec.CLOSING_BY_LEVEL


def test_job_history(stats):
    assert stats["job_history_rows"] == spec.JOB_HISTORY_ROWS
    assert stats["promotions"] == spec.PROMOTIONS == 300
    assert stats["transfers"] == spec.TRANSFERS == 120


def test_compensation_rows_pinned(stats):
    assert stats["compensation_rows"] == spec.COMPENSATION_ROWS
    assert stats["compensation_by_reason"]["hire"] == spec.TOTAL_EMPLOYEES
    assert stats["compensation_by_reason"]["promotion"] == spec.PROMOTIONS


def test_recruiting(stats):
    assert stats["requisitions"] == 1050
    assert stats["requisitions_by_status"] == {"cancelled": 60, "filled": 900, "open": 90}
    assert stats["offers"] == 1080
    assert stats["offers_accepted"] == 900
    assert stats["offers_declined"] == 180
    assert stats["candidates"] == 1080
    assert stats["filled_requisitions_match_hires"] is True


def test_requisition_and_offer_dates_are_consistent():
    reqs = {r["requisition_id"]: r for r in read_csv("raw_requisitions.csv")}
    emps = {e["employee_id"]: e for e in read_csv("raw_employees.csv")}
    for r in reqs.values():
        assert "2022-09-01" <= r["opened_date"] <= "2025-12-31"
        if r["status"] == "filled":
            assert r["filled_date"] > r["opened_date"]
        if r["status"] == "open":
            assert r["filled_date"] == "" and r["closed_date"] == ""
    for o in read_csv("raw_offers.csv"):
        r = reqs[o["requisition_id"]]
        assert o["offer_date"] > r["opened_date"]
        assert o["decision_date"] > o["offer_date"]
        if o["decision"] == "accepted":
            hire = emps[o["hired_employee_id"]]
            assert hire["hire_date"] == r["filled_date"]
            assert o["decision_date"] < hire["hire_date"]


def test_job_history_is_contiguous_and_consistent():
    emps = {e["employee_id"]: e for e in read_csv("raw_employees.csv")}
    rows: dict[str, list[dict[str, str]]] = {}
    for j in read_csv("raw_job_history.csv"):
        rows.setdefault(j["employee_id"], []).append(j)
    assert len(rows) == spec.TOTAL_EMPLOYEES
    for eid, hist in rows.items():
        assert hist[0]["change_reason"] == "hire"
        assert hist[0]["effective_date"] == emps[eid]["hire_date"]
        assert hist[-1]["end_date"] == emps[eid]["termination_date"]
        for prev, cur in zip(hist, hist[1:]):
            assert prev["effective_date"] < cur["effective_date"]
            if cur["change_reason"] == "promotion":
                assert spec.JOB_LEVELS.index(cur["job_level"]) == spec.JOB_LEVELS.index(prev["job_level"]) + 1
                assert cur["department"] == prev["department"]
            if cur["change_reason"] == "transfer":
                assert cur["department"] != prev["department"]
                assert cur["job_level"] == prev["job_level"]


def test_no_pii_values_outside_reserved_domains():
    for e in read_csv("raw_employees.csv"):
        assert e["work_email"].endswith("@lumenworks.example")
    for c in read_csv("raw_candidates.csv"):
        assert c["email"].endswith("@mail.example")
