// Bind a validated intent to its compiled catalog entry. The SQL text is never
// built from user input: only parameter values change, and the BigQuery project
// placeholder is swapped for a validated project id.
import { POLICY, metricDimensions } from "../policy/policy";
import { ApiError, type DimensionId, type Policy } from "../policy/types";
import type { DimensionValues, Validated } from "../policy/validate";
import { DIMENSION_VALUES } from "../policy/validate";
import { type CatalogEntry, catalogKey } from "./catalog";

export const ALL = "__all__";
export const PROJECT_PLACEHOLDER = "`peoplelens-project-id`";
const GCP_PROJECT_RE = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;

export type Dialect = "bigquery" | "duckdb";

export type ParamValue = { type: "DATE" | "TIMESTAMP"; value: string } | { type: "ARRAY<STRING>"; value: string[] };

export interface BoundQuery {
  catalogKey: string;
  dialect: Dialect;
  sql: string;
  /** sha256 of the catalog SQL before the project swap (the sidecar allowlist key). */
  sha256: string;
  params: Record<string, ParamValue>;
  entry: CatalogEntry;
}

/** Same entry, same principal, with the given restricted dimensions widened to every value. */
export function widen(v: Validated, dims: readonly DimensionId[]): Validated {
  const filters = { ...v.filters };
  for (const d of dims) delete filters[d];
  return {
    ...v,
    filters,
    scopeDepartments: dims.includes("department") ? "all" : v.scopeDepartments,
    restricted: v.restricted.filter((d) => !dims.includes(d)),
  };
}

function timeValue(date: string, dialect: Dialect): ParamValue {
  return dialect === "bigquery"
    ? { type: "TIMESTAMP", value: `${date} 00:00:00+00:00` }
    : { type: "DATE", value: date };
}

export function bindQuery(
  entry: CatalogEntry,
  v: Validated,
  dialect: Dialect,
  projectId?: string,
  policy: Policy = POLICY,
  values: DimensionValues = DIMENSION_VALUES,
): BoundQuery {
  const key = catalogKey(v);
  if (entry.key !== key) throw new ApiError(500, "INTERNAL", "catalog entry does not match the intent");
  const params: Record<string, ParamValue> = {
    start_ts: timeValue(v.timeRange.start, dialect),
    end_ts: timeValue(v.timeRange.end, dialect),
  };
  for (const d of metricDimensions(policy, v.metric)) {
    const f = v.filters[d];
    params[`f_${d}`] = { type: "ARRAY<STRING>", value: f && f.length > 0 ? [...f] : [ALL] };
  }
  params["scope_departments"] = {
    type: "ARRAY<STRING>",
    value: v.scopeDepartments === "all" ? [...values.department] : [...v.scopeDepartments],
  };
  const declared = entry.params.map((p) => p.name);
  const bound = Object.keys(params);
  if (declared.length !== bound.length || declared.some((n) => !bound.includes(n))) {
    throw new ApiError(500, "INTERNAL", "parameter contract mismatch");
  }
  let sql = entry.sql;
  if (dialect === "bigquery") {
    if (!projectId || !GCP_PROJECT_RE.test(projectId)) throw new ApiError(503, "CONFIG_INVALID", "GCP_PROJECT_ID is invalid");
    if (!sql.includes(PROJECT_PLACEHOLDER)) throw new ApiError(500, "INTERNAL", "catalog SQL lacks the project placeholder");
    sql = sql.split(PROJECT_PLACEHOLDER).join(`\`${projectId}\``);
  }
  return { catalogKey: key, dialect, sql, sha256: entry.sha256, params, entry };
}
