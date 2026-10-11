import { describe, expect, it } from "vitest";
import { catalogKey, type CatalogFile } from "../src/catalog/catalog";
import duckJson from "../src/generated/catalog.duckdb.json";
import { type BudgetPort, checkCost, rowUpperBound, timeBuckets, utcDay } from "../src/guard/cost";
import { POLICY } from "../src/policy/policy";
import type { Principal, QueryIntent } from "../src/policy/types";
import { DIMENSION_VALUES, type Validated, validateIntent } from "../src/policy/validate";
import { ANALYST, EXEC, HRBP } from "./helpers/principals";

const duck = duckJson as unknown as CatalogFile;
const MIB = 1024 * 1024;

function validated(p: Principal, i: Partial<QueryIntent>): Validated {
  const r = validateIntent(
    { metric: "headcount", groupBy: [], grain: "none", timeRange: { start: "2025-12-01", end: "2025-12-31" }, filters: [], ...i },
    p,
  );
  if (!r.ok) throw new Error(r.violation.message);
  return r.value;
}

class MemoryBudget implements BudgetPort {
  used = 0;
  calls: [string, number, number][] = [];
  async reserveBytes(day: string, bytes: number, cap: number) {
    this.calls.push([day, bytes, cap]);
    if (this.used + bytes > cap) return { ok: false, used: this.used, cap };
    this.used += bytes;
    return { ok: true, used: this.used, cap };
  }
}

describe("row upper bound", () => {
  it("counts time buckets per grain", () => {
    const year = { start: "2023-01-01", end: "2025-12-31" };
    expect(timeBuckets("none", year)).toBe(1);
    expect(timeBuckets("month", year)).toBe(36);
    expect(timeBuckets("quarter", year)).toBe(12);
    expect(timeBuckets("year", year)).toBe(3);
    expect(timeBuckets("quarter", { start: "2024-02-15", end: "2024-11-20" })).toBe(4);
  });

  it("multiplies cardinalities, honoring filters and scope", () => {
    const blowUp = validated(ANALYST, { groupBy: ["department", "job_level"], grain: "month", timeRange: { start: "2023-01-01", end: "2025-12-31" } });
    expect(rowUpperBound(duck.entries[catalogKey(blowUp)]!, blowUp, DIMENSION_VALUES)).toBe(8 * 6 * 36);
    const filtered = validated(ANALYST, {
      groupBy: ["department", "location"],
      filters: [{ dimension: "location", values: ["London", "Austin"] }],
    });
    expect(rowUpperBound(duck.entries[catalogKey(filtered)]!, filtered, DIMENSION_VALUES)).toBe(8 * 2);
    const scoped = validated(HRBP, { groupBy: ["department"], grain: "quarter", timeRange: { start: "2025-01-01", end: "2025-12-31" } });
    expect(rowUpperBound(duck.entries[catalogKey(scoped)]!, scoped, DIMENSION_VALUES)).toBe(2 * 4);
    expect(POLICY.limits.max_rows).toBe(500);
  });
});

describe("checkCost", () => {
  const now = Date.UTC(2026, 9, 10, 12);

  it("reserves the main query plus probes against the daily budget", async () => {
    const budget = new MemoryBudget();
    const d = await checkCost([10 * MIB, 10 * MIB, 10 * MIB], EXEC, POLICY.limits, budget, now);
    expect(d).toEqual({ ok: true, reservedBytes: 30 * MIB, day: "2026-10-10", perQueryCap: 100 * MIB });
    expect(budget.calls).toEqual([["2026-10-10", 30 * MIB, 5 * 1024 * MIB]]);
  });

  it("rejects any single query over the role's per-query cap before reserving", async () => {
    const budget = new MemoryBudget();
    const d = await checkCost([10 * MIB, 101 * MIB], EXEC, POLICY.limits, budget, now);
    expect(d.ok).toBe(false);
    if (!d.ok) expect(d.code).toBe("COST_LIMIT_EXCEEDED");
    expect(budget.calls).toEqual([]);
    const analyst = await checkCost([150 * MIB], ANALYST, POLICY.limits, budget, now);
    expect(analyst.ok).toBe(true); // people_analytics has a 200 MiB cap
  });

  it("refuses when the daily budget is exhausted", async () => {
    const budget = new MemoryBudget();
    budget.used = 5 * 1024 * MIB - 5 * MIB;
    const d = await checkCost([10 * MIB], EXEC, POLICY.limits, budget, now);
    expect(d.ok).toBe(false);
    if (!d.ok) expect(d.code).toBe("DAILY_BUDGET_EXCEEDED");
  });

  it("uses the UTC day", () => {
    expect(utcDay(Date.UTC(2025, 11, 31, 23, 59))).toBe("2025-12-31");
  });
});
