import { describe, expect, it } from "vitest";
import { applyComplementRule, complementProbes } from "../src/guard/complement";
import { type Cell, cellKey, suppressCells } from "../src/guard/suppression";
import type { QueryIntent } from "../src/policy/types";
import { type Validated, validateIntent } from "../src/policy/validate";
import { ANALYST, HRBP } from "./helpers/principals";
import type { Principal } from "../src/policy/types";

function validated(p: Principal, i: Partial<QueryIntent>): Validated {
  const r = validateIntent(
    { metric: "headcount", groupBy: [], grain: "none", timeRange: { start: "2025-12-01", end: "2025-12-31" }, filters: [], ...i },
    p,
  );
  if (!r.ok) throw new Error(r.violation.message);
  return r.value;
}
function total(n: number): Cell {
  return { key: cellKey({}, [], null), dims: {}, time: null, values: { headcount: n }, cohort: n };
}

describe("complementProbes", () => {
  it("plans no probes for unrestricted questions", () => {
    expect(complementProbes(validated(ANALYST, { groupBy: ["department"] }))).toEqual([]);
  });

  it("plans one probe per non-empty combination of restricted dimensions (at most 3)", () => {
    const one = complementProbes(validated(ANALYST, { filters: [{ dimension: "location", values: ["London"] }] }));
    expect(one.map((p) => p.widened)).toEqual([["location"]]);
    const hrbp = complementProbes(validated(HRBP, { filters: [{ dimension: "location", values: ["London"] }] }));
    expect(hrbp.map((p) => p.widened)).toEqual([["department"], ["location"], ["department", "location"]]);
    expect(hrbp[0]?.validated.scopeDepartments).toBe("all");
    expect(hrbp[2]?.validated.filters).toEqual({});
  });

  it("also compares an HRBP's department filter with the whole scope (at most 5 probes)", () => {
    const one = complementProbes(validated(HRBP, { filters: [{ dimension: "department", values: ["Sales"] }] }));
    expect(one.map((p) => p.widened)).toEqual([["department"], ["department:scope"]]);
    expect(one[1]?.validated.filters).toEqual({});
    expect(one[1]?.validated.scopeDepartments).toEqual(["Engineering", "Sales"]);
    const two = complementProbes(
      validated(HRBP, { filters: [{ dimension: "department", values: ["Sales"] }, { dimension: "location", values: ["London"] }] }),
    );
    expect(two.map((p) => p.widened)).toEqual([
      ["department"], ["location"], ["department", "location"], ["department:scope"], ["location", "department:scope"],
    ]);
    expect(two[3]?.validated.filters).toEqual({ location: ["London"] });
    expect(two[4]?.validated.filters).toEqual({});
    expect(two[4]?.validated.scopeDepartments).toEqual(["Engineering", "Sales"]);
  });
});

describe("applyComplementRule", () => {
  it("blocks the two-question attack: 1,780 total vs 1,772 for everyone but Legal", () => {
    const sevenDepartments = ["Engineering", "Sales", "Customer Success", "Marketing", "Finance", "People", "Operations"];
    const v = validated(ANALYST, { filters: [{ dimension: "department", values: sevenDepartments }] });
    expect(v.restricted).toEqual(["department"]);
    const main = [total(1772)];
    const { suppressKeys } = applyComplementRule(main, [[total(1780)]], { k: 10 });
    const out = suppressCells(main, { dims: [], k: 10, forced: suppressKeys });
    expect(out.rows[0]?.suppressed).toBe(true);
    expect(out.counts.complement).toBe(1);
  });

  it("blocks the HRBP scope chain: Engineering + Sales minus Sales, cell by cell", () => {
    // Bengaluru, under a year of tenure, December 2025 in the seeded data: Engineering 5, Sales 12.
    const dims = ["location", "tenure_band"];
    const cell = (n: number): Cell => {
      const d = { location: "Bengaluru", tenure_band: "<1y" };
      return { key: cellKey(d, dims, null), dims: d, time: null, values: {}, cohort: n };
    };
    const sales = [cell(12)];
    const company = [cell(40)]; // every department: far more than 10 away
    const scope = [cell(17)]; // what the HRBP gets without the Sales filter
    expect(applyComplementRule(sales, [company], { k: 10 }).suppressKeys.size).toBe(0);
    expect(applyComplementRule(sales, [company, scope], { k: 10 }).suppressKeys.size).toBe(1);
  });

  it("leaves cells whose widened counterpart differs by 0 or by k or more", () => {
    expect(applyComplementRule([total(1700)], [[total(1780)]], { k: 10 }).suppressKeys.size).toBe(0);
    expect(applyComplementRule([total(1780)], [[total(1780)]], { k: 10 }).suppressKeys.size).toBe(0);
    expect(applyComplementRule([total(1771)], [[total(1780)]], { k: 10 }).suppressKeys.size).toBe(1);
    expect(applyComplementRule([total(1770)], [[total(1780)]], { k: 10 }).suppressKeys.size).toBe(0);
  });

  it("compares group-by cells key by key across every probe", () => {
    const dims = ["department"];
    const cell = (d: string, n: number): Cell => ({ key: cellKey({ department: d }, dims, null), dims: { department: d }, time: null, values: {}, cohort: n });
    const main = [cell("Sales", 50), cell("Engineering", 300)];
    const probeA = [cell("Sales", 53), cell("Engineering", 400)];
    const probeB = [cell("Sales", 50), cell("Engineering", 305)];
    const { suppressKeys } = applyComplementRule(main, [probeA, probeB], { k: 10 });
    expect([...suppressKeys].sort()).toEqual([cell("Engineering", 0).key, cell("Sales", 0).key].sort());
  });
});
