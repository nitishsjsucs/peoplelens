"""Disclosure rounding (SPEC.md 9.5 rule 7) against the attack it closes.

Suppression keeps every shown cohort at 10 or more, but two allowed questions can still
differ by one person. The HRBP (scope Engineering + Sales) asks compensation by
department and job level, monthly for 2025, and headcount of the same shape (the cohort
sizes). Wherever a cohort grows by exactly one person between adjacent months,
n2 x avg2 - n1 x avg1 is that person's salary plus any raises. This test replays the
attack on the seeded warehouse through the semantic view (what the Worker queries), first
on raw averages, then on averages rounded the way the Worker rounds them."""

from __future__ import annotations

import math
import statistics

import duckdb

from peoplelens.policy import load

K = load().limits["min_cohort"]
STEP = load().metrics["compensation_summary"]["disclosure_round"]["avg_base_salary"]


def worker_round(v: float) -> float:
    """JavaScript Math.round(v / step) * step, as worker/src/guard/disclosure.ts does."""
    return math.floor(v / STEP + 0.5) * STEP


def test_compensation_is_rounded_to_a_thousand_dollars_in_the_policy():
    comp = load().metrics["compensation_summary"]
    assert comp["disclosure_round"] == {"avg_base_salary": 1000, "median_base_salary": 1000}
    assert all("disclosure_round" not in m for mid, m in load().metrics.items() if mid != "compensation_summary")
    assert worker_round(274_300.0) == 274_000 and worker_round(151_500.0) == 152_000


def test_two_question_salary_differencing_no_longer_recovers_a_salary(require_warehouse):
    con = duckdb.connect(str(require_warehouse), read_only=True)
    cells = con.sql("""
        select department, job_level, cast(month_start as date) as m,
               sum(is_active_eom) as n,
               avg(case when is_active_eom = 1 then base_salary_eom end) as avg_salary
        from semantic.sem_workforce_monthly
        where department in ('Engineering', 'Sales') and month_start >= '2025-01-01' and month_start < '2026-01-01'
        group by 1, 2, 3 order by 1, 2, 3""").fetchall()
    salaries = {}
    for d, lvl, m, s in con.sql("""
            select department, job_level, cast(month_start as date), base_salary_eom
            from semantic.sem_workforce_monthly where is_active_eom = 1 and month_start >= '2025-01-01'""").fetchall():
        salaries.setdefault((d, lvl, m), set()).add(float(s))
    con.close()
    series: dict[tuple, list] = {}
    for d, lvl, m, n, avg in cells:
        series.setdefault((d, lvl), []).append((m, int(n), float(avg)))

    pairs, exact, errors = 0, 0, []
    for (d, lvl), seq in series.items():
        for (_, n1, a1), (m2, n2, a2) in zip(seq, seq[1:]):
            if n1 < K or n2 - n1 != 1:
                continue
            pairs += 1
            raw = n2 * a2 - n1 * a1
            if not any(abs(raw - s) < 1e-6 for s in salaries[(d, lvl, m2)]):
                continue  # a raise or a leaver in the same month blurs the raw attack too
            exact += 1
            errors.append(abs(n2 * worker_round(a2) - n1 * worker_round(a1) - raw))
    # The attack is real on unrounded values: many pairs give one active employee's exact salary.
    assert pairs >= 20 and exact >= 10, (pairs, exact)
    # Rounded the way the Worker rounds, the same arithmetic misses every one of those salaries
    # by more than $1,000, and by thousands of dollars typically.
    assert min(errors) > 1_000 and statistics.median(errors) > 5_000, sorted(errors)
