"""Per-metric comparison tolerances (policy/metrics.json `tolerance`)."""

from __future__ import annotations

import math


def value_ok(rule: dict, actual: float | None, expected: float | None,
             band_low: float | None = None, band_high: float | None = None, rounded_to: float = 0.0) -> bool:
    """True when `actual` matches `expected` under the rule.

    For approximate medians pass the rank band (gold median_band_low and
    median_band_high): the value must lie in [band_low x lower_factor, band_high x upper_factor].
    `rounded_to` is the Worker's disclosure rounding step for this measure (policy
    `disclosure_round`, 1,000 for salaries): a value it rounded may sit up to half a step
    outside the rule. Warehouse parity checks never pass it.
    """
    if expected is None or actual is None:
        return expected is None and actual is None
    slack = rounded_to / 2 if rounded_to else 0.0
    kind = rule["type"]
    if kind == "exact":
        return actual == expected if not slack else abs(actual - expected) <= slack
    if kind == "abs":
        return abs(actual - expected) <= rule["value"] + slack
    if kind == "rel":
        if not slack:
            return math.isclose(actual, expected, rel_tol=rule["value"], abs_tol=1e-12)
        return abs(actual - expected) <= rule["value"] * max(abs(actual), abs(expected)) + slack
    if kind == "median_band":
        lo = (band_low if band_low is not None else expected) * rule["lower_factor"]
        hi = (band_high if band_high is not None else expected) * rule["upper_factor"]
        return lo - slack <= actual <= hi + slack
    raise ValueError(f"unknown tolerance type {kind!r}")
