// The full 4 x 6 metric and 4 x 5 dimension allow/deny matrix, written out
// independently of policy/roles.json, enforced through validateIntent.
import { describe, expect, it } from "vitest";
import { POLICY, entityDimensions } from "../src/policy/policy";
import { DIMENSION_IDS, METRIC_IDS, type MetricId, ROLE_IDS, type RoleId } from "../src/policy/types";
import { validateIntent } from "../src/policy/validate";
import { principal } from "./helpers/principals";

const METRICS: Record<RoleId, MetricId[]> = {
  people_analytics: ["headcount", "fte", "turnover_rate", "offer_acceptance_rate", "time_to_fill_days", "compensation_summary"],
  hr_business_partner: ["headcount", "fte", "turnover_rate", "compensation_summary"],
  talent_acquisition: ["headcount", "offer_acceptance_rate", "time_to_fill_days"],
  executive: ["headcount", "fte", "turnover_rate", "offer_acceptance_rate", "time_to_fill_days", "compensation_summary"],
};
const DIMS: Record<RoleId, string[]> = {
  people_analytics: ["department", "location", "job_level", "employment_type", "tenure_band"],
  hr_business_partner: ["department", "location", "job_level", "tenure_band"],
  talent_acquisition: ["department", "location", "job_level"],
  executive: ["department", "location"],
};
const RANGE = { start: "2025-01-01", end: "2025-12-31" };

describe("governance matrix", () => {
  it("has exactly six metrics, five dimensions and four roles", () => {
    expect(METRIC_IDS).toHaveLength(6);
    expect(DIMENSION_IDS).toHaveLength(5);
    expect(ROLE_IDS).toHaveLength(4);
    expect(Object.keys(POLICY.metrics).sort()).toEqual([...METRIC_IDS].sort());
    expect(Object.keys(POLICY.roles).sort()).toEqual([...ROLE_IDS].sort());
    expect(POLICY.limits.min_cohort).toBe(10);
  });

  for (const role of ROLE_IDS) {
    for (const metric of METRIC_IDS) {
      const allowed = METRICS[role].includes(metric);
      it(`${role} ${allowed ? "may" : "may not"} query ${metric}`, () => {
        const r = validateIntent({ metric, groupBy: [], grain: "none", timeRange: RANGE, filters: [] }, principal(role));
        expect(r.ok).toBe(allowed);
        if (!r.ok) expect(r.violation.code).toBe("METRIC_NOT_ALLOWED");
      });
      if (!allowed) continue;
      for (const dim of DIMENSION_IDS) {
        const ok = DIMS[role].includes(dim) && entityDimensions(POLICY, metric).includes(dim);
        it(`${role} ${ok ? "may" : "may not"} break ${metric} down by ${dim}`, () => {
          const r = validateIntent({ metric, groupBy: [dim], grain: "none", timeRange: RANGE, filters: [] }, principal(role));
          expect(r.ok).toBe(ok);
          if (!r.ok) expect(r.violation.code).toBe("DIMENSION_NOT_APPROVED");
        });
      }
    }
  }

  it("the matrix in this test equals policy/roles.json", () => {
    for (const role of ROLE_IDS) {
      expect([...POLICY.roles[role].metrics].sort()).toEqual([...METRICS[role]].sort());
      expect([...POLICY.roles[role].dimensions].sort()).toEqual([...DIMS[role]].sort());
    }
  });
});
