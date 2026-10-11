// Governance vocabulary shared by every module. Values mirror policy/*.json;
// policy.test and the matrix tests assert that the two never drift.

export const METRIC_IDS = [
  "headcount",
  "fte",
  "turnover_rate",
  "offer_acceptance_rate",
  "time_to_fill_days",
  "compensation_summary",
] as const;
export type MetricId = (typeof METRIC_IDS)[number];

export const DIMENSION_IDS = ["department", "location", "job_level", "employment_type", "tenure_band"] as const;
export type DimensionId = (typeof DIMENSION_IDS)[number];

export const GRAINS = ["none", "month", "quarter", "year"] as const;
export type Grain = (typeof GRAINS)[number];

export const ROLE_IDS = ["people_analytics", "hr_business_partner", "talent_acquisition", "executive"] as const;
export type RoleId = (typeof ROLE_IDS)[number];

export type Entity = "workforce" | "requisition" | "offer";

/**
 * Codes the planner's JSON schema offers the model (unchanged since v1, so the
 * schema the token counts in SPEC.md 25.5 were measured on stays identical).
 */
export const PLANNER_REFUSAL_CODES = [
  "UNAUTHENTICATED",
  "NO_ROLE_BINDING",
  "METRIC_NOT_ALLOWED",
  "DIMENSION_NOT_APPROVED",
  "PII_REQUEST",
  "WRITE_REQUEST",
  "SCOPE_VIOLATION",
  "GRAIN_NOT_ALLOWED",
  "TIME_RANGE_INVALID",
  "RESULT_TOO_LARGE",
  "COST_LIMIT_EXCEEDED",
  "DAILY_BUDGET_EXCEEDED",
  "RATE_LIMITED",
  "OUT_OF_DOMAIN",
  "PLANNER_INVALID_OUTPUT",
] as const;
export type PlannerRefusalCode = (typeof PLANNER_REFUSAL_CODES)[number];

/**
 * Every refusal code the API can return. AI_DAILY_LIMIT (HTTP 503 with Retry-After
 * to 00:00 UTC) is server-only: this repo's Workers AI share for the UTC day is used
 * up, or Workers AI answered 3036. The model can never choose it.
 */
export const REFUSAL_CODES = [...PLANNER_REFUSAL_CODES, "AI_DAILY_LIMIT"] as const;
export type RefusalCode = (typeof REFUSAL_CODES)[number];

export interface MetricDef {
  id: MetricId;
  label: string;
  family: "workforce" | "recruiting";
  kind: "stock" | "flow";
  entity: Entity;
  mf_metrics: string[];
  cohort_metric: string;
  unit: string;
  definition: string;
  default_range: { start: string; end: string };
  aliases: string[];
}

export interface DimensionDef {
  id: DimensionId;
  label: string;
  values: string[];
  aliases: Record<string, string>;
  mf_names: Partial<Record<Entity, string>>;
}

export interface NotApprovedDef {
  id: string;
  code: "DIMENSION_NOT_APPROVED" | "PII_REQUEST";
  aliases: string[];
}

export interface RoleDef {
  id: RoleId;
  label: string;
  metrics: MetricId[];
  dimensions: DimensionId[];
  grains: Grain[];
  scope: "all" | "assigned_departments";
}

export interface Limits {
  min_cohort: number;
  max_group_by: number;
  max_restricted_dimensions: number;
  max_rows: number;
  max_range_months: number;
  data_start: string;
  data_end: string;
  as_of: string;
  per_query_byte_cap: Record<RoleId, number>;
  bigquery_min_bytes_per_table: number;
  daily_byte_budget: number;
  rate_limit_per_minute: number;
  gateway_rate_limit_per_minute: number;
  eval_pacing_per_minute: number;
  dry_run_cache_seconds: number;
  query_timeout_ms: number;
}

export interface Policy {
  metrics: Record<MetricId, MetricDef>;
  dimensions: Record<DimensionId, DimensionDef>;
  notApproved: NotApprovedDef[];
  roles: Record<RoleId, RoleDef>;
  limits: Limits;
}

export interface Scope {
  departments: string[] | "all";
}

export interface Principal {
  subject: string;
  kind: "user" | "service_token";
  role: RoleId;
  scope: Scope;
}

export interface TimeRange {
  start: string;
  end: string;
}

export interface Filter {
  dimension: DimensionId;
  values: string[];
}

export interface QueryIntent {
  metric: MetricId;
  groupBy: DimensionId[];
  grain: Grain;
  timeRange: TimeRange;
  filters: Filter[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: RefusalCode | "CONFIG_INVALID" | "BAD_REQUEST" | "NOT_FOUND" | "INTERNAL",
    message: string,
    readonly alternatives: string[] = [],
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function isMetricId(v: unknown): v is MetricId {
  return typeof v === "string" && (METRIC_IDS as readonly string[]).includes(v);
}
export function isDimensionId(v: unknown): v is DimensionId {
  return typeof v === "string" && (DIMENSION_IDS as readonly string[]).includes(v);
}
export function isGrain(v: unknown): v is Grain {
  return typeof v === "string" && (GRAINS as readonly string[]).includes(v);
}
export function isRoleId(v: unknown): v is RoleId {
  return typeof v === "string" && (ROLE_IDS as readonly string[]).includes(v);
}
export function isRefusalCode(v: unknown): v is RefusalCode {
  return typeof v === "string" && (REFUSAL_CODES as readonly string[]).includes(v);
}
