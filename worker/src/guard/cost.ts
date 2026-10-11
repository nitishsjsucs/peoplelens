// Query-cost limits, enforced before execution (SPEC.md sections 3.3 step 7 and 7.3):
//  1. row upper bound from dimension cardinalities x time buckets (max_rows)
//  2. dry-run bytes of the main query and each complement probe against the
//     role's per-query cap (BigQuery dry run in production, local estimate otherwise)
//  3. reserve the total against the user's daily byte budget in UserDO
// Execution then carries maximumBytesBilled = the role cap, so BigQuery itself
// enforces the cap as a backstop.
import type { CatalogEntry } from "../catalog/catalog";
import type { Grain, Limits, Principal, TimeRange } from "../policy/types";
import type { DimensionValues, Validated } from "../policy/validate";

export interface BudgetDecision {
  ok: boolean;
  used: number;
  cap: number;
}

export interface BudgetPort {
  reserveBytes(day: string, bytes: number, cap: number): Promise<BudgetDecision>;
}

export type CostDecision =
  | { ok: true; reservedBytes: number; day: string; perQueryCap: number }
  | { ok: false; code: "COST_LIMIT_EXCEEDED" | "DAILY_BUDGET_EXCEEDED"; message: string };

function ym(date: string): [number, number] {
  const [y, m] = date.split("-").map(Number) as [number, number];
  return [y, m];
}

export function timeBuckets(grain: Grain, range: TimeRange): number {
  const [sy, sm] = ym(range.start);
  const [ey, em] = ym(range.end);
  switch (grain) {
    case "none":
      return 1;
    case "month":
      return (ey - sy) * 12 + (em - sm) + 1;
    case "quarter":
      return (ey - sy) * 4 + (Math.floor((em - 1) / 3) - Math.floor((sm - 1) / 3)) + 1;
    case "year":
      return ey - sy + 1;
  }
}

/** Upper bound on result rows: product of possible values per group-by dimension x time buckets. */
export function rowUpperBound(entry: CatalogEntry, v: Validated, values: DimensionValues): number {
  let rows = timeBuckets(entry.grain, v.timeRange);
  for (const d of entry.group_by) {
    let n = v.filters[d]?.length ?? values[d].length;
    if (d === "department" && v.scopeDepartments !== "all") {
      const scoped = v.scopeDepartments;
      n = v.filters.department ? v.filters.department.filter((x) => scoped.includes(x)).length : scoped.length;
    }
    rows *= n;
  }
  return rows;
}

export function utcDay(nowMs: number): string {
  return new Date(nowMs).toISOString().slice(0, 10);
}

/**
 * `estimates` holds the dry-run bytes of the main query followed by each probe.
 * Every query must fit the role's per-query cap; their sum is reserved against
 * the daily budget.
 */
export async function checkCost(
  estimates: readonly number[],
  p: Principal,
  limits: Limits,
  budget: BudgetPort,
  nowMs: number,
): Promise<CostDecision> {
  const cap = limits.per_query_byte_cap[p.role];
  for (const est of estimates) {
    if (!Number.isFinite(est) || est < 0 || est > cap) {
      return {
        ok: false,
        code: "COST_LIMIT_EXCEEDED",
        message: `This question would scan more than your per-query limit of ${Math.round(cap / 1048576)} MiB.`,
      };
    }
  }
  const total = estimates.reduce((s, e) => s + e, 0);
  const day = utcDay(nowMs);
  const decision = await budget.reserveBytes(day, total, limits.daily_byte_budget);
  if (!decision.ok) {
    return {
      ok: false,
      code: "DAILY_BUDGET_EXCEEDED",
      message: "Your daily query budget is used up. It resets at 00:00 UTC.",
    };
  }
  return { ok: true, reservedBytes: total, day, perQueryCap: cap };
}
