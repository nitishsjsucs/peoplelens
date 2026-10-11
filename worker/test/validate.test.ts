import { describe, expect, it } from "vitest";
import { POLICY } from "../src/policy/policy";
import type { QueryIntent } from "../src/policy/types";
import { type ValidationResult, validateIntent } from "../src/policy/validate";
import { ANALYST, EXEC, HRBP, RECRUITER } from "./helpers/principals";

function intent(p: Partial<QueryIntent> = {}): QueryIntent {
  return {
    metric: "headcount",
    groupBy: [],
    grain: "none",
    timeRange: { start: "2025-12-01", end: "2025-12-31" },
    filters: [],
    ...p,
  };
}
function code(r: ValidationResult): string {
  return r.ok ? "OK" : r.violation.code;
}

describe("validateIntent", () => {
  it("accepts a plain question and normalizes it", () => {
    const r = validateIntent(intent({ groupBy: ["location", "department"] }), ANALYST);
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.value.groupBy).toEqual(["department", "location"]);
      expect(r.value.restricted).toEqual([]);
      expect(r.value.scopeDepartments).toBe("all");
    }
  });

  it("refuses dimensions that are not approved for the role or metric", () => {
    expect(code(validateIntent(intent({ groupBy: ["job_level"] }), EXEC))).toBe("DIMENSION_NOT_APPROVED");
    expect(code(validateIntent(intent({ metric: "time_to_fill_days", groupBy: ["tenure_band"], timeRange: { start: "2025-01-01", end: "2025-12-31" } }), ANALYST))).toBe("DIMENSION_NOT_APPROVED");
    const gender = intent({ groupBy: ["gender" as never] });
    const r = validateIntent(gender, ANALYST);
    expect(code(r)).toBe("DIMENSION_NOT_APPROVED");
    if (!r.ok) expect(r.violation.alternatives).toEqual(["department", "location", "job_level", "employment_type", "tenure_band"]);
    expect(code(validateIntent(intent({ filters: [{ dimension: "employment_type", values: ["part_time"] }] }), HRBP))).toBe("DIMENSION_NOT_APPROVED");
  });

  it("refuses metrics outside the role", () => {
    expect(code(validateIntent(intent({ metric: "compensation_summary" }), RECRUITER))).toBe("METRIC_NOT_ALLOWED");
    expect(code(validateIntent(intent({ metric: "offer_acceptance_rate" }), HRBP))).toBe("METRIC_NOT_ALLOWED");
    expect(code(validateIntent(intent({ metric: "salary" as never }), ANALYST))).toBe("OUT_OF_DOMAIN");
  });

  it("turns a disallowed grain into a clarification", () => {
    const r = validateIntent(intent({ grain: "month", timeRange: { start: "2025-01-01", end: "2025-12-31" } }), EXEC);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.violation.code).toBe("GRAIN_NOT_ALLOWED");
      expect(r.violation.clarify?.question).toBe("Monthly is not available to your role. Show quarterly instead?");
      expect(r.violation.clarify?.options.map((o) => o.patch.grain)).toEqual(["quarter", "year"]);
    }
  });

  it("checks the time range", () => {
    const bad = [
      { start: "2025-02-30", end: "2025-03-01" },
      { start: "2025-06-01", end: "2025-01-01" },
      { start: "2022-01-01", end: "2022-12-31" },
      { start: "2025-01-01", end: "2026-01-31" },
      { start: "2025/01/01", end: "2025-12-31" },
    ];
    for (const timeRange of bad) expect(code(validateIntent(intent({ timeRange }), ANALYST))).toBe("TIME_RANGE_INVALID");
    const tight = { ...POLICY, limits: { ...POLICY.limits, max_range_months: 12 } };
    expect(code(validateIntent(intent({ timeRange: { start: "2024-01-01", end: "2025-06-30" } }), ANALYST, tight))).toBe("TIME_RANGE_INVALID");
    expect(code(validateIntent(intent({ timeRange: { start: "2023-01-01", end: "2025-12-31" } }), ANALYST))).toBe("OK");
  });

  it("caps group-by at two dimensions", () => {
    expect(code(validateIntent(intent({ groupBy: ["department", "location", "job_level"] }), ANALYST))).toBe("RESULT_TOO_LARGE");
  });

  it("canonicalizes values and aliases, and rejects unknown values", () => {
    const r = validateIntent(intent({ groupBy: ["location"], filters: [{ dimension: "location", values: ["SJ", "bangalore", "London"] }] }), ANALYST);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.value.filters.location).toEqual(["Bengaluru", "London", "San Jose"]);
    const e = validateIntent(intent({ groupBy: ["department"], filters: [{ dimension: "department", values: ["eng", "CS"] }] }), ANALYST);
    expect(e.ok).toBe(true);
    if (e.ok) expect(e.value.filters.department).toEqual(["Customer Success", "Engineering"]);
    expect(code(validateIntent(intent({ filters: [{ dimension: "location", values: ["Paris"] }] }), ANALYST))).toBe("OUT_OF_DOMAIN");
    expect(code(validateIntent(intent({ filters: [{ dimension: "location", values: ["x' OR 1=1 --"] }] }), ANALYST))).toBe("OUT_OF_DOMAIN");
  });

  it("allows one value or all but one on a dimension that is not a breakdown (no two-question differencing)", () => {
    // Engineering or Legal (628) minus Engineering (620) would reveal Legal (8 people) in two questions.
    const pair = validateIntent(intent({ filters: [{ dimension: "department", values: ["Engineering", "Legal"] }] }), EXEC);
    expect(code(pair)).toBe("DIMENSION_NOT_APPROVED");
    if (!pair.ok) expect(pair.violation.message).toContain("by department");
    const three = intent({ filters: [{ dimension: "location", values: ["London", "Austin", "New York"] }] });
    expect(code(validateIntent(three, ANALYST))).toBe("DIMENSION_NOT_APPROVED");
    // As a breakdown every row is one value, so any set is fine.
    expect(code(validateIntent({ ...three, groupBy: ["location"] }, ANALYST))).toBe("OK");
    // One value, and "everyone except Legal" (7 of 8), are allowed; the complement rule compares
    // the exclusion with its widened counterpart.
    expect(code(validateIntent(intent({ filters: [{ dimension: "department", values: ["Legal"] }] }), EXEC))).toBe("OK");
    const allButLegal = POLICY.dimensions.department.values.filter((v) => v !== "Legal");
    const except = validateIntent(intent({ filters: [{ dimension: "department", values: allButLegal }] }), EXEC);
    expect(code(except)).toBe("OK");
    if (except.ok) expect(except.value.restricted).toEqual(["department"]);
    // An HRBP department filter equal to the scope is the scope, not a two-value filter.
    const scope = validateIntent(intent({ filters: [{ dimension: "department", values: ["Sales", "Engineering"] }] }), HRBP);
    expect(code(scope)).toBe("OK");
    if (scope.ok) {
      expect(scope.value.filters.department).toBeUndefined();
      expect(scope.value.restricted).toEqual(["department"]);
      expect(scope.value.intent.filters).toEqual([]);
    }
  });

  it("drops filters that keep every value and intersects repeated filters", () => {
    const all = ["San Jose", "New York", "Austin", "London", "Bengaluru"];
    const r = validateIntent(intent({ filters: [{ dimension: "location", values: all }] }), ANALYST);
    if (r.ok) {
      expect(r.value.filters.location).toBeUndefined();
      expect(r.value.restricted).toEqual([]);
    }
    const twice = validateIntent(
      intent({ filters: [{ dimension: "location", values: ["London", "Austin"] }, { dimension: "location", values: ["London"] }] }),
      ANALYST,
    );
    if (twice.ok) expect(twice.value.filters.location).toEqual(["London"]);
  });

  it("keeps HRBPs inside their assigned departments", () => {
    const outside = validateIntent(intent({ filters: [{ dimension: "department", values: ["Legal"] }] }), HRBP);
    expect(code(outside)).toBe("SCOPE_VIOLATION");
    const inside = validateIntent(intent({ groupBy: ["department"], filters: [{ dimension: "department", values: ["Sales"] }] }), HRBP);
    expect(inside.ok).toBe(true);
    if (inside.ok) {
      expect(inside.value.scopeDepartments).toEqual(["Engineering", "Sales"]);
      expect(inside.value.restricted).toEqual(["department"]);
    }
    const scoped = validateIntent(intent({ groupBy: ["location"] }), HRBP);
    if (scoped.ok) expect(scoped.value.restricted).toEqual(["department"]);
  });

  it("allows at most two restricted dimensions (scope counts as department)", () => {
    const three = intent({
      filters: [
        { dimension: "location", values: ["London"] },
        { dimension: "job_level", values: ["L1"] },
        { dimension: "department", values: ["Sales"] },
      ],
    });
    expect(code(validateIntent(three, ANALYST))).toBe("DIMENSION_NOT_APPROVED");
    const hrbpTwo = intent({ filters: [{ dimension: "location", values: ["London"] }, { dimension: "job_level", values: ["L1"] }] });
    expect(code(validateIntent(hrbpTwo, HRBP))).toBe("DIMENSION_NOT_APPROVED");
    const analystTwo = intent({ filters: [{ dimension: "location", values: ["London"] }, { dimension: "job_level", values: ["L1"] }] });
    expect(code(validateIntent(analystTwo, ANALYST))).toBe("OK");
  });
});
