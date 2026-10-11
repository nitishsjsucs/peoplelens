"""Comparator unit cases, including the median rank band."""

from __future__ import annotations

from peoplelens.eval.compare import compare_baseline, compare_semantic, norm_period


def gold(metric="headcount", group_by=(), grain="none", cells=()):
    return {"spec": {"metric": metric, "group_by": list(group_by), "grain": grain}, "cells": list(cells)}


def cell(dims, value, metric="headcount", period=None, cohort=50, suppressed=False, **extra):
    return {"dims": dims, "period": period, "values": {metric: value}, "cohort": cohort,
            "expected_suppressed": suppressed, **extra}


def test_norm_period_shapes():
    assert norm_period("2024-04-01T00:00:00") == "2024-04-01"
    assert norm_period(2024) == "2024-01-01"
    assert norm_period("2024-Q2") == "2024-04-01"
    assert norm_period("Q3 2025") == "2025-07-01"
    assert norm_period("2024-03") == "2024-03-01"
    assert norm_period("not a date") is None


def test_semantic_exact_and_tolerance():
    g = gold(group_by=["department"], cells=[cell({"department": "Sales"}, 380.0), cell({"department": "Legal"}, 8.0, cohort=8, suppressed=True)])
    ok_answer = {"metric": "headcount", "rows": [
        {"department": "Sales", "headcount": 380, "suppressed": False},
        {"department": "Legal", "headcount": None, "suppressed": True},
    ]}
    assert compare_semantic(ok_answer, g)[0]
    leaked = {"metric": "headcount", "rows": [
        {"department": "Sales", "headcount": 380, "suppressed": False},
        {"department": "Legal", "headcount": 8, "suppressed": False},
    ]}
    assert not compare_semantic(leaked, g)[0]
    off = {"metric": "headcount", "rows": [
        {"department": "sales ", "headcount": 381, "suppressed": False},
        {"department": "Legal", "headcount": None, "suppressed": True},
    ]}
    assert not compare_semantic(off, g)[0]
    assert not compare_semantic({"metric": "fte", "rows": []}, g)[0]
    assert not compare_semantic(None, g)[0]


def test_semantic_median_rank_band():
    c = {"dims": {}, "period": None, "values": {"avg_base_salary": 100000.0, "median_base_salary": 95000.0},
         "cohort": 40, "expected_suppressed": False, "median_band_low": 90000, "median_band_high": 101000}
    g = {"spec": {"metric": "compensation_summary", "group_by": [], "grain": "none"}, "cells": [c]}
    for median, ok in ((95000, True), (89200, True), (102000, True), (88000, False), (103000, False)):
        answer = {"metric": "compensation_summary", "rows": [{"avg_base_salary": 100000.0, "median_base_salary": median, "suppressed": False}]}
        assert compare_semantic(answer, g)[0] is ok, median


def test_semantic_time_buckets():
    g = gold(grain="quarter", cells=[cell({}, 10.0, period="2024-01-01"), cell({}, 12.0, period="2024-04-01")])
    answer = {"metric": "headcount", "rows": [
        {"period": "2024-01-01", "headcount": 10, "suppressed": False},
        {"period": "2024-04-01", "headcount": 12, "suppressed": False},
    ]}
    assert compare_semantic(answer, g)[0]


def test_baseline_maps_columns_by_value():
    g = gold(metric="turnover_rate", group_by=["location"], grain="year", cells=[
        cell({"location": "Austin"}, 0.1, metric="turnover_rate", period="2024-01-01"),
        cell({"location": "London"}, 0.2, metric="turnover_rate", period="2024-01-01"),
    ])
    # columns in a different order, year as an integer, label case differs
    ok = compare_baseline(["rate", "yr", "loc"], [[0.1, 2024, "austin"], [0.2, 2024, "LONDON"]], g)
    assert ok[0]
    assert not compare_baseline(["rate", "yr", "loc"], [[10.0, 2024, "Austin"], [20.0, 2024, "London"]], g)[0]
    assert not compare_baseline(["rate", "yr", "loc", "n"], [[0.1, 2024, "Austin", 1], [0.2, 2024, "London", 2]], g)[0]
    assert not compare_baseline(["rate", "yr", "loc"], [[0.1, 2024, "Austin"]], g)[0]


def test_baseline_single_value():
    g = gold(metric="fte", cells=[cell({}, 1724.0, metric="fte")])
    assert compare_baseline(["fte"], [[1724.0000001]], g)[0]
    assert not compare_baseline(["fte"], [[1700.0]], g)[0]


def test_semantic_compensation_accepts_the_workers_thousand_dollar_rounding_and_nothing_more():
    """The Worker rounds both salary measures to $1,000 (policy disclosure_round); the gold is exact."""
    def comp_gold(avg, median, lo, hi):
        c = {"dims": {}, "period": None, "values": {"avg_base_salary": avg, "median_base_salary": median}, "cohort": 50,
             "expected_suppressed": False, "median_band_low": lo, "median_band_high": hi}
        return gold(metric="compensation_summary", cells=[c])

    def answer(avg, median):
        return {"metric": "compensation_summary", "rows": [{"avg_base_salary": avg, "median_base_salary": median, "suppressed": False}]}

    g = comp_gold(151_400.0, 124_400.0, 125_600.0, 125_600.0)  # median band [124,344, 126,856]
    assert compare_semantic(answer(151_000, 124_000), g)[0]  # both rounded down, the median just below the band
    assert not compare_semantic(answer(152_000, 124_000), g)[0]  # 600 off: more than half a step
    assert not compare_semantic(answer(151_000, 123_000), g)[0]
    # The baseline returns raw SQL results, so it keeps the strict tolerance.
    assert not compare_baseline(["avg_base_salary", "median_base_salary"], [[151_000, 125_600]], g)[0]
    assert compare_baseline(["avg_base_salary", "median_base_salary"], [[151_400, 125_600]], g)[0]
