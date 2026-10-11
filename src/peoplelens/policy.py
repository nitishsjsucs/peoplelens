"""Loader for policy/*.json (metrics, dimensions, roles, limits)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from peoplelens.paths import POLICY_DIR

GRAINS = ("none", "month", "quarter", "year")


@dataclass(frozen=True)
class Policy:
    metrics: dict[str, dict]
    dimensions: dict[str, dict]
    not_approved: tuple[dict, ...]
    roles: dict[str, dict]
    limits: dict

    def metric_dimensions(self, metric_id: str) -> tuple[str, ...]:
        """Approved dimension union for a metric: dimensions any role may use with it,
        restricted to the dimensions the metric's entity has."""
        entity = self.metrics[metric_id]["entity"]
        allowed: list[str] = []
        for dim_id, dim in self.dimensions.items():
            if entity not in dim["mf_names"]:
                continue
            if any(metric_id in r["metrics"] and dim_id in r["dimensions"] for r in self.roles.values()):
                allowed.append(dim_id)
        return tuple(allowed)

    def merged(self) -> dict:
        return {
            "metrics": list(self.metrics.values()),
            "dimensions": list(self.dimensions.values()),
            "not_approved": list(self.not_approved),
            "roles": list(self.roles.values()),
            "limits": self.limits,
        }


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=2)
def load(policy_dir: str = str(POLICY_DIR)) -> Policy:
    d = Path(policy_dir)
    metrics = _read(d / "metrics.json")["metrics"]
    dims = _read(d / "dimensions.json")
    roles = _read(d / "roles.json")["roles"]
    limits = _read(d / "limits.json")
    return Policy(
        metrics={m["id"]: m for m in metrics},
        dimensions={x["id"]: x for x in dims["dimensions"]},
        not_approved=tuple(dims["not_approved"]),
        roles={r["id"]: r for r in roles},
        limits=limits,
    )


def load_role_bindings(path: Path) -> dict[str, dict]:
    return _read(path)
