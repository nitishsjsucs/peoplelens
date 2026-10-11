"""MetricFlow names for governed dimensions and time."""

from __future__ import annotations

from peoplelens.policy import Policy

# The time-dimension grain used in the time-range filter, per entity.
FILTER_TIME_GRAIN = {"workforce": "month", "requisition": "day", "offer": "day"}


def mf_dimension(policy: Policy, metric_id: str, dim_id: str) -> str:
    entity = policy.metrics[metric_id]["entity"]
    return policy.dimensions[dim_id]["mf_names"][entity]


def mf_time(grain: str) -> str | None:
    return None if grain == "none" else f"metric_time__{grain}"


def query_metrics(policy: Policy, metric_id: str) -> list[str]:
    """MetricFlow metrics requested for a governed metric: returned metrics plus the cohort metric."""
    m = policy.metrics[metric_id]
    names = list(m["mf_metrics"])
    if m["cohort_metric"] not in names:
        names.append(m["cohort_metric"])
    return names
