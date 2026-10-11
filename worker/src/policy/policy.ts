import policyJson from "../generated/policy.json";
import {
  DIMENSION_IDS,
  type DimensionDef,
  type DimensionId,

  type Limits,
  METRIC_IDS,
  type MetricDef,
  type MetricId,
  type NotApprovedDef,
  type Policy,
  ROLE_IDS,
  type RoleDef,
  type RoleId,
  isDimensionId,
  isGrain,
  isMetricId,
  isRoleId,
} from "./types";

interface RawPolicy {
  metrics: unknown[];
  dimensions: unknown[];
  not_approved: unknown[];
  roles: unknown[];
  limits: unknown;
}

function fail(message: string): never {
  throw new Error(`generated/policy.json is invalid: ${message}`);
}

/** Parse and index the generated policy (a merged copy of policy/*.json). */
export function loadPolicy(raw: unknown): Policy {
  const p = raw as RawPolicy;
  if (!p || !Array.isArray(p.metrics) || !Array.isArray(p.dimensions) || !Array.isArray(p.roles)) {
    fail("missing sections");
  }
  const metrics = {} as Record<MetricId, MetricDef>;
  for (const m of p.metrics as MetricDef[]) {
    if (!isMetricId(m.id)) fail(`unknown metric ${String(m.id)}`);
    metrics[m.id] = m;
  }
  const dimensions = {} as Record<DimensionId, DimensionDef>;
  for (const d of p.dimensions as DimensionDef[]) {
    if (!isDimensionId(d.id)) fail(`unknown dimension ${String(d.id)}`);
    dimensions[d.id] = d;
  }
  const roles = {} as Record<RoleId, RoleDef>;
  for (const r of p.roles as RoleDef[]) {
    if (!isRoleId(r.id)) fail(`unknown role ${String(r.id)}`);
    for (const m of r.metrics) if (!isMetricId(m)) fail(`role ${r.id}: unknown metric ${m}`);
    for (const d of r.dimensions) if (!isDimensionId(d)) fail(`role ${r.id}: unknown dimension ${d}`);
    for (const g of r.grains) if (!isGrain(g)) fail(`role ${r.id}: unknown grain ${g}`);
    roles[r.id] = r;
  }
  for (const id of METRIC_IDS) if (!metrics[id]) fail(`missing metric ${id}`);
  for (const id of DIMENSION_IDS) if (!dimensions[id]) fail(`missing dimension ${id}`);
  for (const id of ROLE_IDS) if (!roles[id]) fail(`missing role ${id}`);
  return {
    metrics,
    dimensions,
    notApproved: p.not_approved as NotApprovedDef[],
    roles,
    limits: p.limits as Limits,
  };
}

export const POLICY: Policy = loadPolicy(policyJson);

/** Dimensions the metric's entity has (department, location, job_level for recruiting). */
export function entityDimensions(policy: Policy, metric: MetricId): DimensionId[] {
  const entity = policy.metrics[metric].entity;
  return DIMENSION_IDS.filter((d) => policy.dimensions[d].mf_names[entity] !== undefined);
}

/** Approved dimension union for a metric across roles (the catalog's parameter set). */
export function metricDimensions(policy: Policy, metric: MetricId): DimensionId[] {
  return entityDimensions(policy, metric).filter((d) =>
    ROLE_IDS.some((r) => policy.roles[r].metrics.includes(metric) && policy.roles[r].dimensions.includes(d)),
  );
}

/** Dimensions a role may use with a metric. */
export function roleDimensions(policy: Policy, role: RoleId, metric: MetricId): DimensionId[] {
  return entityDimensions(policy, metric).filter((d) => policy.roles[role].dimensions.includes(d));
}

export function byteCap(policy: Policy, role: RoleId): number {
  return policy.limits.per_query_byte_cap[role];
}
