"""policy/*.json: exactly six metrics, four roles, five approved dimensions, k = 10."""

from __future__ import annotations

import json

from peoplelens.paths import GENERATED_DIR, POLICY_DIR
from peoplelens.policy import load, load_role_bindings
from peoplelens.reference import metrics as reference
from peoplelens.synth import spec

POLICY = load()

ALL_METRICS = {"headcount", "fte", "turnover_rate", "offer_acceptance_rate", "time_to_fill_days", "compensation_summary"}
ALL_DIMS = {"department", "location", "job_level", "employment_type", "tenure_band"}


def test_exactly_six_governed_metrics():
    assert set(POLICY.metrics) == ALL_METRICS
    assert len(POLICY.metrics) == 6


def test_metric_definitions_are_complete():
    for mid, m in POLICY.metrics.items():
        assert m["entity"] in ("workforce", "requisition", "offer")
        assert m["mf_metrics"] and m["cohort_metric"]
        assert set(m["tolerance"]) == set(m["mf_metrics"]), mid
        assert set(m.get("disclosure_round") or {}) <= set(m["mf_metrics"]), mid
        assert all(step > 0 for step in (m.get("disclosure_round") or {}).values()), mid
        assert m["definition"] and m["aliases"]
        ref_metrics, ref_cohort, _ = reference.GOVERNED[mid]
        assert tuple(m["mf_metrics"]) == ref_metrics and m["cohort_metric"] == ref_cohort


def test_median_rank_band_matches_reference_constants():
    rule = POLICY.metrics["compensation_summary"]["tolerance"]["median_base_salary"]
    assert rule["rank_fraction"] == reference.MEDIAN_RANK_FRACTION
    assert rule["rank_min"] == reference.MEDIAN_RANK_MIN


def test_exactly_four_roles_with_the_spec_matrix():
    assert len(POLICY.roles) == 4
    expected = {
        "people_analytics": (ALL_METRICS, ALL_DIMS, {"none", "month", "quarter", "year"}, "all"),
        "hr_business_partner": (
            {"headcount", "fte", "turnover_rate", "compensation_summary"},
            {"department", "location", "job_level", "tenure_band"},
            {"none", "month", "quarter", "year"},
            "assigned_departments",
        ),
        "talent_acquisition": (
            {"headcount", "offer_acceptance_rate", "time_to_fill_days"},
            {"department", "location", "job_level"},
            {"none", "month", "quarter", "year"},
            "all",
        ),
        "executive": (ALL_METRICS, {"department", "location"}, {"none", "quarter", "year"}, "all"),
    }
    for rid, (metrics, dims, grains, scope) in expected.items():
        role = POLICY.roles[rid]
        assert set(role["metrics"]) == metrics, rid
        assert set(role["dimensions"]) == dims, rid
        assert set(role["grains"]) == grains, rid
        assert role["scope"] == scope, rid


def test_five_approved_dimensions_with_values_matching_the_data():
    assert set(POLICY.dimensions) == ALL_DIMS
    assert tuple(POLICY.dimensions["department"]["values"]) == spec.DEPARTMENTS
    assert tuple(POLICY.dimensions["location"]["values"]) == spec.LOCATIONS
    assert tuple(POLICY.dimensions["job_level"]["values"]) == spec.JOB_LEVELS
    assert tuple(POLICY.dimensions["employment_type"]["values"]) == spec.EMPLOYMENT_TYPES
    assert tuple(POLICY.dimensions["tenure_band"]["values"]) == spec.TENURE_BANDS
    for dim in POLICY.dimensions.values():
        for alias, target in dim["aliases"].items():
            assert alias == alias.lower() and target in dim["values"], (dim["id"], alias)


def test_recruiting_metrics_support_only_department_location_level():
    for mid in ("offer_acceptance_rate", "time_to_fill_days"):
        assert POLICY.metric_dimensions(mid) == ("department", "location", "job_level")
    for mid in ("headcount", "fte", "turnover_rate", "compensation_summary"):
        assert set(POLICY.metric_dimensions(mid)) == ALL_DIMS


def test_sensitive_attributes_are_never_approved():
    banned = {x["id"] for x in POLICY.not_approved}
    assert {"gender", "date_of_birth", "age", "name", "email", "employee_id", "candidate_id", "job_family", "manager"} <= banned
    assert not banned & set(POLICY.dimensions)
    for x in POLICY.not_approved:
        assert x["code"] in ("DIMENSION_NOT_APPROVED", "PII_REQUEST")


def test_limits():
    lim = POLICY.limits
    assert lim["min_cohort"] == 10
    assert lim["max_group_by"] == 2
    assert lim["max_restricted_dimensions"] == 2
    assert lim["max_rows"] == 500
    assert lim["max_range_months"] == 36
    assert lim["per_query_byte_cap"]["people_analytics"] == 200 * 1024 * 1024
    for rid in ("hr_business_partner", "talent_acquisition", "executive"):
        assert lim["per_query_byte_cap"][rid] == 100 * 1024 * 1024
    assert set(lim["per_query_byte_cap"]) == set(POLICY.roles)
    assert lim["daily_byte_budget"] == 5 * 1024**3
    assert lim["rate_limit_per_minute"] == 30
    assert lim["eval_pacing_per_minute"] == 25 < lim["rate_limit_per_minute"]
    assert lim["as_of"] == spec.AS_OF.isoformat()


def test_local_role_bindings_cover_all_four_roles():
    bindings = load_role_bindings(POLICY_DIR / "role_bindings.local.json")
    assert {b["role"] for b in bindings.values()} == set(POLICY.roles)
    for key, b in bindings.items():
        assert key.startswith("email:") and key.endswith("@example.test") and key == key.lower()
    hrbp = bindings["email:hrbp@example.test"]
    assert hrbp["scope"]["departments"] == ["Engineering", "Sales"]


def test_generated_policy_is_the_merged_policy_files():
    generated = json.loads((GENERATED_DIR / "policy.json").read_text())
    assert generated == json.loads(json.dumps(POLICY.merged()))
