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
