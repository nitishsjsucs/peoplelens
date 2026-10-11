// Server-side policy enforcement for a planned query intent. Every check here is
// independent of the LLM: whatever the planner produced, only approved metrics,
// dimensions, values, grains, time ranges and scopes reach the catalog.
import valuesJson from "../generated/dimension_values.json";
import { POLICY, roleDimensions } from "./policy";
import {
  type DimensionId,
  type Filter,
  type Grain,
  type MetricId,
  type Policy,
  type Principal,
  type QueryIntent,
  type RefusalCode,
  type TimeRange,
  isDimensionId,
  isGrain,
  isMetricId,
} from "./types";

export type DimensionValues = Record<DimensionId, string[]>;
export const DIMENSION_VALUES = valuesJson as DimensionValues;

export interface Validated {
  metric: MetricId;
  groupBy: DimensionId[]; // sorted, unique
  grain: Grain;
  timeRange: TimeRange;
  /** Effective filter values per dimension (canonical labels, sorted); absent = unfiltered. */
  filters: Partial<Record<DimensionId, string[]>>;
  /** Principal scope (HRBP assigned departments), or "all". */
  scopeDepartments: string[] | "all";
  /** Dimensions restricted to a proper subset by a filter or the scope (at most max_restricted_dimensions). */
  restricted: DimensionId[];
  /** The normalized intent, as returned to the client and stored in the session. */
  intent: QueryIntent;
}

export interface ClarifyOption {
  id: string;
  label: string;
  patch: Partial<QueryIntent>;
}

export interface Violation {
  code: RefusalCode;
  message: string;
  alternatives: string[];
  clarify?: { question: string; options: ClarifyOption[] };
}

export type ValidationResult = { ok: true; value: Validated } | { ok: false; violation: Violation };

const ISO = /^\d{4}-\d{2}-\d{2}$/;

function isIsoDate(s: unknown): s is string {
  if (typeof s !== "string" || !ISO.test(s)) return false;
  const d = new Date(`${s}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}

function monthsSpanned(start: string, end: string): number {
  const [sy, sm] = start.split("-").map(Number) as [number, number];
  const [ey, em] = end.split("-").map(Number) as [number, number];
  return (ey - sy) * 12 + (em - sm) + 1;
}

/** Canonical label for a raw value: exact, case-insensitive, or an alias. */
export function canonicalValue(policy: Policy, dim: DimensionId, raw: string): string | undefined {
  const def = policy.dimensions[dim];
  const needle = raw.trim().toLowerCase();
  const exact = def.values.find((v) => v.toLowerCase() === needle);
  if (exact) return exact;
  return def.aliases[needle];
}

function refuse(code: RefusalCode, message: string, alternatives: string[] = []): ValidationResult {
  return { ok: false, violation: { code, message, alternatives } };
}

const GRAIN_LABEL: Record<Grain, string> = { none: "a single total", month: "monthly", quarter: "quarterly", year: "yearly" };

export function validateIntent(
  intent: QueryIntent,
  p: Principal,
  policy: Policy = POLICY,
  values: DimensionValues = DIMENSION_VALUES,
): ValidationResult {
  const role = policy.roles[p.role];
  const limits = policy.limits;

  // 1. metric
  if (!isMetricId(intent.metric)) return refuse("OUT_OF_DOMAIN", "That is not one of the governed workforce metrics.", role.metrics);
  if (!role.metrics.includes(intent.metric)) {
    return refuse(
      "METRIC_NOT_ALLOWED",
      `${policy.metrics[intent.metric].label} is not available to your role.`,
      role.metrics.map((m) => policy.metrics[m].label),
    );
  }
  const metric = intent.metric;
  const allowedDims = roleDimensions(policy, p.role, metric);

  // 2. group-by
  const groupBy = [...new Set(intent.groupBy ?? [])];
  for (const d of groupBy) {
    if (!isDimensionId(d) || !allowedDims.includes(d)) {
      return refuse(
        "DIMENSION_NOT_APPROVED",
        `That breakdown of ${policy.metrics[metric].label} is not approved for your role.`,
        allowedDims,
      );
    }
  }
  if (groupBy.length > limits.max_group_by) {
    return refuse(
      "RESULT_TOO_LARGE",
      `At most ${limits.max_group_by} breakdown dimensions are allowed in one question (time is extra).`,
      allowedDims,
    );
  }
  groupBy.sort();

  // 3. grain
  if (!isGrain(intent.grain)) return refuse("GRAIN_NOT_ALLOWED", "Unknown time grain.", role.grains);
  if (!role.grains.includes(intent.grain)) {
    const want = GRAIN_LABEL[intent.grain];
    const wantCap = want.charAt(0).toUpperCase() + want.slice(1);
    const options: ClarifyOption[] = role.grains
      .filter((g) => g !== "none")
      .map((g, i) => ({ id: `g${i + 1}`, label: `Show ${GRAIN_LABEL[g]}`, patch: { grain: g } }));
    const suggestion = options[0] ? options[0].label.replace(/^Show /, "") : GRAIN_LABEL.none;
    return {
      ok: false,
      violation: {
        code: "GRAIN_NOT_ALLOWED",
        message: `${wantCap} is not available to your role.`,
        alternatives: role.grains,
        clarify: { question: `${wantCap} is not available to your role. Show ${suggestion} instead?`, options },
      },
    };
  }

  // 4. time range
  const tr = intent.timeRange;
  if (!tr || !isIsoDate(tr.start) || !isIsoDate(tr.end) || tr.start > tr.end) {
    return refuse("TIME_RANGE_INVALID", "The time range is not valid.", [`${limits.data_start} to ${limits.data_end}`]);
  }
  if (tr.start < limits.data_start || tr.end > limits.data_end) {
    return refuse(
      "TIME_RANGE_INVALID",
      `Data is available from ${limits.data_start} to ${limits.data_end}.`,
      [`${limits.data_start} to ${limits.data_end}`],
    );
  }
  if (monthsSpanned(tr.start, tr.end) > limits.max_range_months) {
    return refuse("TIME_RANGE_INVALID", `Ranges are limited to ${limits.max_range_months} months.`);
  }

  // 5. filters: approved dimensions, canonical values, intersect repeats
  const filters: Partial<Record<DimensionId, string[]>> = {};
  for (const f of (intent.filters ?? []) as Filter[]) {
    if (!isDimensionId(f.dimension) || !allowedDims.includes(f.dimension)) {
      return refuse(
        "DIMENSION_NOT_APPROVED",
        `That filter on ${policy.metrics[metric].label} is not approved for your role.`,
        allowedDims,
      );
    }
    const dim = f.dimension;
    if (!Array.isArray(f.values) || f.values.length === 0) continue;
    const canon: string[] = [];
    for (const raw of f.values) {
      const c = typeof raw === "string" ? canonicalValue(policy, dim, raw) : undefined;
      if (!c || !values[dim].includes(c)) {
        return refuse("OUT_OF_DOMAIN", `One of the requested values is not a known ${policy.dimensions[dim].label.toLowerCase()}.`, values[dim]);
      }
      if (!canon.includes(c)) canon.push(c);
    }
    const prev = filters[dim];
    filters[dim] = (prev ? canon.filter((v) => prev.includes(v)) : canon).sort();
    if (filters[dim]?.length === 0) {
      return refuse("OUT_OF_DOMAIN", `The ${policy.dimensions[dim].label.toLowerCase()} filters exclude every value.`);
    }
  }
  // A filter that keeps every value is no filter.
  for (const d of Object.keys(filters) as DimensionId[]) {
    if (filters[d]?.length === values[d].length) delete filters[d];
  }

  // 6. scope (HRBP): department filter must stay inside the assigned departments
  const scope = p.scope.departments;
  if (scope !== "all") {
    const deptFilter = filters.department;
    if (deptFilter) {
      const outside = deptFilter.filter((d) => !scope.includes(d));
      if (outside.length > 0) {
        return refuse(
          "SCOPE_VIOLATION",
          `Your access covers ${scope.join(" and ")}; ${outside.join(", ")} is outside it.`,
          scope,
        );
      }
    }
  }

  // 7. restricted dimensions (filters and scope merge into one restricted department)
  const restricted = new Set<DimensionId>(Object.keys(filters) as DimensionId[]);
  if (scope !== "all" && scope.length < values.department.length) restricted.add("department");
  if (restricted.size > limits.max_restricted_dimensions) {
    return refuse(
      "DIMENSION_NOT_APPROVED",
      `Filtering on more than ${limits.max_restricted_dimensions} dimensions at once is not approved.`,
      [...restricted].sort(),
    );
  }

  const normalized: QueryIntent = {
    metric,
    groupBy,
    grain: intent.grain,
    timeRange: { start: tr.start, end: tr.end },
    filters: (Object.keys(filters) as DimensionId[])
      .sort()
      .map((d) => ({ dimension: d, values: filters[d] ?? [] })),
  };
  return {
    ok: true,
    value: {
      metric,
      groupBy,
      grain: intent.grain,
      timeRange: normalized.timeRange,
      filters,
      scopeDepartments: scope === "all" ? "all" : [...scope].sort(),
      restricted: [...restricted].sort(),
      intent: normalized,
    },
  };
}
