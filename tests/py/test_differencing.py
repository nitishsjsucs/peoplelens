"""Two-question differencing on the seeded data, through the Python port of the Worker's
suppression (eval/gold.py and eval/suppression.py mirror worker/src/guard/*.ts).

The HRBP (scope Engineering + Sales) can ask for the whole scope and for one department of
it. "Headcount by location and tenure band" minus "the same for Sales" is Engineering, cell
by cell. Before the scope probe, Bengaluru with under a year of tenure was shown in both
answers (17 and 12) and gave Engineering's 5 people."""

from __future__ import annotations

from peoplelens.eval.gold import gold_for_spec, probe_specs

BASE = {"metric": "headcount", "group_by": ["location", "tenure_band"], "grain": "none",
        "start": "2025-12-01", "end": "2025-12-31", "scope": ["Engineering", "Sales"]}
CELL = {"location": "Bengaluru", "tenure_band": "<1y"}


def cell(spec: dict) -> dict:
    return next(c for c in gold_for_spec(spec)["cells"] if c["dims"] == CELL)


def test_a_department_filter_inside_the_hrbp_scope_is_compared_with_the_scope():
    whole_scope = cell({**BASE, "filters": {}})
    sales = cell({**BASE, "filters": {"department": ["Sales"]}})
    assert (whole_scope["cohort"], sales["cohort"]) == (17, 12)
    assert not whole_scope["expected_suppressed"]
    assert sales["expected_suppressed"] and sales["suppression_reason"] == "complement"


def test_scope_probes_are_planned_like_the_worker():
    one = probe_specs({**BASE, "group_by": [], "filters": {"department": ["Sales"]}})
    assert [(p["filters"], p.get("scope")) for p in one] == [({}, None), ({}, ["Engineering", "Sales"])]
    two = probe_specs({**BASE, "group_by": [], "filters": {"department": ["Sales"], "location": ["London"]}})
    assert len(two) == 5
    assert [(p["filters"], p.get("scope")) for p in two[3:]] == [({"location": ["London"]}, ["Engineering", "Sales"]),
                                                               ({}, ["Engineering", "Sales"])]
    # No department filter: only the widened-to-every-value probes, as before.
    assert len(probe_specs({**BASE, "filters": {"location": ["London"]}})) == 3
