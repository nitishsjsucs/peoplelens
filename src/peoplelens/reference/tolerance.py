"""Per-metric comparison tolerances (policy/metrics.json `tolerance`)."""

from __future__ import annotations

import math


def value_ok(rule: dict, actual: float | None, expected: float | None,
             band_low: float | None = None, band_high: float | None = None) -> bool:
    """True when `actual` matches `expected` under the rule.

    For approximate medians pass the rank band (gold median_band_low and
    median_band_high): the value must lie in [band_low x lower_factor, band_high x upper_factor].
    """
    if expected is None or actual is None:
        return expected is None and actual is None
    kind = rule["type"]
    if kind == "exact":
        return actual == expected
    if kind == "abs":
        return abs(actual - expected) <= rule["value"]
    if kind == "rel":
        return math.isclose(actual, expected, rel_tol=rule["value"], abs_tol=1e-12)
    if kind == "median_band":
        lo = (band_low if band_low is not None else expected) * rule["lower_factor"]
        hi = (band_high if band_high is not None else expected) * rule["upper_factor"]
        return lo <= actual <= hi
    raise ValueError(f"unknown tolerance type {kind!r}")
