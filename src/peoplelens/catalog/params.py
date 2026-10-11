"""Parameter contract and where-filters per dialect (SPEC.md section 8).

Every entry takes:
  start_ts, end_ts          TIMESTAMP on BigQuery, DATE on DuckDB
  f_<dim>                   ARRAY<STRING> for each approved dimension of the metric,
                            ['__all__'] when unfiltered (NULL is never bound)
  scope_departments         ARRAY<STRING>, all departments when unscoped
"""

from __future__ import annotations

from datetime import date

from peoplelens.catalog.dims import FILTER_TIME_GRAIN, mf_dimension
from peoplelens.policy import Policy

ALL = "__all__"
DIALECT_PARAM_TYPES = {
    "duckdb": {"time": "DATE", "array": "VARCHAR[]"},
    "bigquery": {"time": "TIMESTAMP", "array": "ARRAY<STRING>"},
}


def param_names(policy: Policy, metric_id: str) -> list[str]:
    dims = policy.metric_dimensions(metric_id)
    return ["start_ts", "end_ts", *[f"f_{d}" for d in dims], "scope_departments"]


def param_contract(policy: Policy, metric_id: str, dialect: str) -> list[dict[str, str]]:
    types = DIALECT_PARAM_TYPES[dialect]
    out = []
    for name in param_names(policy, metric_id):
        out.append({"name": name, "type": types["time"] if name.endswith("_ts") else types["array"]})
    return out


def where_filters(policy: Policy, metric_id: str, dialect: str) -> list[str]:
    entity = policy.metrics[metric_id]["entity"]
    out = []
    for dim in policy.metric_dimensions(metric_id):
        ref = "{{ Dimension('" + mf_dimension(policy, metric_id, dim) + "') }}"
        if dialect == "bigquery":
            out.append(f"('{ALL}' IN UNNEST(@f_{dim}) OR {ref} IN UNNEST(@f_{dim}))")
        else:
            out.append(f"(list_contains($f_{dim}, '{ALL}') OR list_contains($f_{dim}, {ref}))")
    dept = "{{ Dimension('" + mf_dimension(policy, metric_id, "department") + "') }}"
    time = "{{ TimeDimension('metric_time', '" + FILTER_TIME_GRAIN[entity] + "') }}"
    if dialect == "bigquery":
        out.append(f"{dept} IN UNNEST(@scope_departments)")
        out.append(f"{time} BETWEEN @start_ts AND @end_ts")
    else:
        out.append(f"list_contains($scope_departments, {dept})")
        out.append(f"{time} BETWEEN $start_ts AND $end_ts")
    return out


def bind_values(
    policy: Policy,
    metric_id: str,
    start: date,
    end: date,
    filters: dict[str, list[str]] | None = None,
    scope_departments: list[str] | None = None,
) -> dict[str, object]:
    """Parameter values for one execution (Python side: parity tests, smoke, eval)."""
    filters = filters or {}
    values: dict[str, object] = {"start_ts": start, "end_ts": end}
    for dim in policy.metric_dimensions(metric_id):
        values[f"f_{dim}"] = list(filters.get(dim) or [ALL])
    values["scope_departments"] = (
        list(scope_departments) if scope_departments is not None else list(policy.dimensions["department"]["values"])
    )
    return values
