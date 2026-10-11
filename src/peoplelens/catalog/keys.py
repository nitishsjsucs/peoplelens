"""Catalog keys and the set of compiled shapes."""

from __future__ import annotations

from itertools import combinations

from peoplelens.policy import GRAINS, Policy


def catalog_key(metric: str, group_by: tuple[str, ...] | list[str], grain: str) -> str:
    """`metric|sorted(group_by)|grain`, for example `turnover_rate|department|quarter`."""
    return f"{metric}|{','.join(sorted(group_by))}|{grain}"


def shapes(policy: Policy) -> list[tuple[str, tuple[str, ...], str]]:
    """Every (metric, group-by subset of size 0 to max_group_by, grain), in a stable order."""
    max_group_by = policy.limits["max_group_by"]
    out: list[tuple[str, tuple[str, ...], str]] = []
    for metric_id in policy.metrics:
        dims = policy.metric_dimensions(metric_id)
        for size in range(0, max_group_by + 1):
            for subset in combinations(dims, size):
                for grain in GRAINS:
                    out.append((metric_id, tuple(sorted(subset)), grain))
    return out
