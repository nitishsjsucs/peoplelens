"""The reference implementation against hand-computed values on a 12-employee fixture.

Window for the fixture: 2025-01-01 to 2025-03-31. Every expected number below
was worked out by hand from the rows in this file (see comments), not by running
the code, so this test checks the reference independently of dbt and MetricFlow.
"""

from __future__ import annotations

import csv
from datetime import date

import pytest

from peoplelens.reference.metrics import Query, build_facts, evaluate, tenure_band
from peoplelens.synth.generator import COLUMNS

EMPLOYEES = [
    # id, hire, term, term_type, employment_type, fte
    ("E01", "2020-01-01", "", "", "full_time", "1.0"),
    ("E02", "2021-06-01", "", "", "part_time", "0.5"),
    ("E03", "2024-11-15", "", "", "full_time", "1.0"),
    ("E04", "2018-03-01", "2025-02-10", "voluntary", "full_time", "1.0"),
    ("E05", "2025-02-03", "", "", "full_time", "1.0"),
    ("E06", "2019-05-01", "", "", "full_time", "1.0"),
    ("E07", "2022-01-10", "", "", "part_time", "0.8"),
    ("E08", "2023-07-01", "", "", "full_time", "1.0"),
    ("E09", "2016-02-01", "2025-03-31", "involuntary", "full_time", "1.0"),
    ("E10", "2025-01-20", "2025-03-05", "voluntary", "full_time", "1.0"),
    ("E11", "2010-01-01", "2024-12-31", "voluntary", "full_time", "1.0"),
    ("E12", "2024-01-31", "", "", "full_time", "1.0"),
]

JOBS = [
    # id, effective, end, department, location, level, reason
    ("E01", "2020-01-01", "", "Engineering", "San Jose", "L3", "hire"),
    ("E02", "2021-06-01", "", "Engineering", "San Jose", "L2", "hire"),
    ("E03", "2024-11-15", "", "Engineering", "New York", "L1", "hire"),
    ("E04", "2018-03-01", "2025-02-10", "Sales", "New York", "L4", "hire"),
    ("E05", "2025-02-03", "", "Sales", "San Jose", "L2", "hire"),
    ("E06", "2019-05-01", "2025-02-28", "Sales", "San Jose", "L5", "hire"),
    ("E06", "2025-03-01", "", "Sales", "San Jose", "L6", "promotion"),
    ("E07", "2022-01-10", "", "Legal", "London", "L3", "hire"),
    ("E08", "2023-07-01", "2025-01-31", "Engineering", "London", "L2", "hire"),
    ("E08", "2025-02-01", "", "Sales", "London", "L2", "transfer"),
    ("E09", "2016-02-01", "2025-03-31", "Engineering", "San Jose", "L4", "hire"),
    ("E10", "2025-01-20", "2025-03-05", "Engineering", "San Jose", "L1", "hire"),
    ("E11", "2010-01-01", "2024-12-31", "Finance", "Austin", "L3", "hire"),
    ("E12", "2024-01-31", "", "Finance", "Austin", "L2", "hire"),
]

COMP = [
    ("E01", "2020-01-01", "100000", "hire"),
    ("E02", "2021-06-01", "80000", "hire"),
    ("E03", "2024-11-15", "70000", "hire"),
    ("E04", "2018-03-01", "150000", "hire"),
    ("E05", "2025-02-03", "90000", "hire"),
    ("E06", "2019-05-01", "200000", "hire"),
    ("E06", "2025-03-01", "230000", "promotion"),
    ("E07", "2022-01-10", "120000", "hire"),
    ("E08", "2023-07-01", "95000", "hire"),
    ("E09", "2016-02-01", "160000", "hire"),
    ("E10", "2025-01-20", "65000", "hire"),
    ("E11", "2010-01-01", "110000", "hire"),
    ("E12", "2024-01-31", "85000", "hire"),
]

REQS = [
    ("R1", "Engineering", "San Jose", "L1", "2024-12-01", "filled", "2025-01-20", "2025-01-20"),
    ("R2", "Sales", "San Jose", "L2", "2024-12-20", "filled", "2025-02-03", "2025-02-03"),
    ("R3", "Marketing", "Austin", "L2", "2025-01-15", "open", "", ""),
    ("R4", "Finance", "Austin", "L3", "2024-11-01", "cancelled", "", "2024-12-15"),
]

OFFERS = [
    ("O1", "R1", "C1", "2025-01-05", "accepted", "2025-01-07", "E10"),
    ("O2", "R2", "C2", "2025-01-10", "declined", "2025-01-12", ""),
    ("O3", "R2", "C3", "2025-01-15", "accepted", "2025-01-17", "E05"),
    ("O4", "R3", "C4", "2025-02-01", "declined", "2025-02-03", ""),
]

Q1 = (date(2025, 1, 1), date(2025, 3, 31))
MARCH = (date(2025, 3, 1), date(2025, 3, 31))


def _write(path, name, rows, cols):
    with open(path / name, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)


@pytest.fixture(scope="module")
def facts(tmp_path_factory):
    d = tmp_path_factory.mktemp("fixture_seeds")
    emp_rows = [(i, f"Name {i}", f"{i.lower()}@x.example", "1990-01-01", "X", h, t, tt, et, f)
                for i, h, t, tt, et, f in EMPLOYEES]
    job_rows = [(i, eff, end, dep, loc, lvl, "Family", why) for i, eff, end, dep, loc, lvl, why in JOBS]
    _write(d, "raw_employees.csv", emp_rows, COLUMNS["raw_employees.csv"])
    _write(d, "raw_job_history.csv", job_rows, COLUMNS["raw_job_history.csv"])
    _write(d, "raw_compensation_history.csv", COMP, COLUMNS["raw_compensation_history.csv"])
    _write(d, "raw_requisitions.csv", REQS, COLUMNS["raw_requisitions.csv"])
    _write(d, "raw_offers.csv", OFFERS, COLUMNS["raw_offers.csv"])
    return build_facts(d, date(2025, 1, 1), date(2025, 3, 31))


def _by(cells, dim):
    return {c.dims[dim]: c for c in cells}


def test_monthly_rows(facts):
    # E11 left before the window: no rows. E05 joins in Feb. E04 leaves in Feb (still has a Feb row).
    per_emp = {}
    for r in facts.workforce:
        per_emp.setdefault(r.month_start, 0)
        per_emp[r.month_start] += 1
    # Jan: E01-E04, E06-E10, E12 = 10. Feb: Jan set + E05 = 11. Mar: Feb set minus E04 = 10.
    assert per_emp == {date(2025, 1, 1): 10, date(2025, 2, 1): 11, date(2025, 3, 1): 10}


def test_headcount_at_month_end(facts):
    # Active on 2025-03-31: E01 E02 E03 E05 E06 E07 E08 E12 (E09 left on 03-31, not active that day).
    [cell] = evaluate(Query("headcount", (), "none", *MARCH), facts)
    assert cell.values["headcount"] == 8 and cell.cohort == 8


def test_headcount_by_department_includes_groups_with_only_leavers(facts):
    cells = _by(evaluate(Query("headcount", ("department",), "none", *MARCH), facts), "department")
    assert {k: c.values["headcount"] for k, c in cells.items()} == {
        "Engineering": 3, "Sales": 3, "Legal": 1, "Finance": 1,
    }


def test_headcount_by_month_and_quarter(facts):
    by_month = {c.period: c.values["headcount"] for c in evaluate(Query("headcount", (), "month", *Q1), facts)}
    # Jan: 10 (E05 not hired yet). Feb: E04 left, E05 joined -> 10. Mar: 8.
    assert by_month == {"2025-01-01": 10, "2025-02-01": 10, "2025-03-01": 8}
    [q] = evaluate(Query("headcount", (), "quarter", *Q1), facts)
    assert q.period == "2025-01-01" and q.values["headcount"] == 8  # last month of the quarter


def test_fte(facts):
    [cell] = evaluate(Query("fte", (), "none", *MARCH), facts)
    # 1 + 0.5 + 1 + 1 + 1 + 0.8 + 1 + 1
    assert cell.values["fte"] == pytest.approx(7.3)
    assert cell.cohort == 8


def test_turnover(facts):
    [cell] = evaluate(Query("turnover_rate", (), "none", *Q1), facts)
    # terminations E04 (Feb), E09 (Mar), E10 (Mar) = 3; employee months 10 + 10 + 8 = 28; 3 months.
    assert cell.values["turnover_rate"] == pytest.approx(3 / (28 / 3))
    assert cell.cohort == pytest.approx(28 / 3)
    cells = _by(evaluate(Query("turnover_rate", ("department",), "none", *Q1), facts), "department")
    # Engineering: actives Jan 6 (E01 E02 E03 E08 E09 E10), Feb 5, Mar 3 = 14; leavers E09, E10.
    assert cells["Engineering"].values["turnover_rate"] == pytest.approx(2 / (14 / 3))
    # Sales: Jan 2 (E04 E06), Feb 3 (E05 E06 E08), Mar 3 = 8; leaver E04.
    assert cells["Sales"].values["turnover_rate"] == pytest.approx(1 / (8 / 3))


def test_compensation_summary(facts):
    [cell] = evaluate(Query("compensation_summary", (), "none", *MARCH), facts)
    # 70k 80k 85k 90k 95k 100k 120k 230k (E06 promoted to 230k on 03-01)
    assert cell.values["avg_base_salary"] == pytest.approx(870000 / 8)
    assert (cell.median_lower, cell.median_upper) == (90000, 95000)
    # Rank band: one rank either side of the middle values (n = 8, 2% of n rounds up to 1).
    assert (cell.median_band_low, cell.median_band_high) == (85000, 100000)
    assert cell.values["median_base_salary"] == pytest.approx(92500)


def test_promotion_and_transfer_are_effective_dated(facts):
    feb = _by(evaluate(Query("headcount", ("department",), "none", date(2025, 2, 1), date(2025, 2, 28)), facts), "department")
    jan = _by(evaluate(Query("headcount", ("department",), "none", date(2025, 1, 1), date(2025, 1, 31)), facts), "department")
    # E08 moves from Engineering to Sales on 2025-02-01.
    assert jan["Engineering"].values["headcount"] == 6
    assert feb["Sales"].values["headcount"] == 3
    levels = _by(evaluate(Query("headcount", ("job_level",), "none", *MARCH), facts), "job_level")
    assert levels["L6"].values["headcount"] == 1 and "L5" not in levels


def test_tenure_bands(facts):
    cells = _by(evaluate(Query("headcount", ("tenure_band",), "none", *MARCH), facts), "tenure_band")
    assert {k: c.values["headcount"] for k, c in cells.items() if c.values["headcount"]} == {
        "<1y": 2, "1-2y": 2, "2-5y": 2, "5y+": 2,
    }
    assert tenure_band(364) == "<1y" and tenure_band(365) == "1-2y"
    assert tenure_band(1825) == "2-5y" and tenure_band(1826) == "5y+"


def test_filters_and_scope(facts):
    [london] = evaluate(Query("headcount", (), "none", *MARCH, filters=(("location", ("London",)),)), facts)
    assert london.values["headcount"] == 2  # E07, E08
    scoped = _by(
        evaluate(Query("headcount", ("department",), "none", *MARCH, scope_departments=("Sales", "Legal")), facts),
        "department",
    )
    assert {k: c.values["headcount"] for k, c in scoped.items()} == {"Sales": 3, "Legal": 1}


def test_time_to_fill(facts):
    [cell] = evaluate(Query("time_to_fill_days", (), "none", *Q1), facts)
    assert cell.values["time_to_fill_days"] == pytest.approx((50 + 45) / 2)
    assert cell.cohort == 2


def test_offer_acceptance(facts):
    [cell] = evaluate(Query("offer_acceptance_rate", (), "none", *Q1), facts)
    assert cell.values["offer_acceptance_rate"] == pytest.approx(0.5) and cell.cohort == 4
    cells = _by(evaluate(Query("offer_acceptance_rate", ("department",), "none", *Q1), facts), "department")
    assert cells["Sales"].values["offer_acceptance_rate"] == pytest.approx(0.5)
    assert cells["Engineering"].values["offer_acceptance_rate"] == pytest.approx(1.0)
    assert cells["Marketing"].values["offer_acceptance_rate"] == pytest.approx(0.0)


def test_unknown_dimension_for_recruiting_is_rejected(facts):
    with pytest.raises(ValueError):
        evaluate(Query("time_to_fill_days", ("tenure_band",), "none", *Q1), facts)
