import { describe, expect, it } from "vitest";
import { type Cell, cellKey, suppress, suppressCells, toCells } from "../src/guard/suppression";

const K = 10;

function cells(dims: string[], rows: [Record<string, string>, number, (string | null)?][]): Cell[] {
  return rows.map(([d, cohort, time]) => ({
    key: cellKey(d, dims, time ?? null),
    dims: d,
    time: time ?? null,
    values: { headcount: cohort },
    cohort,
  }));
}

const CLOSING_BY_DEPT: [string, number][] = [
  ["Engineering", 620], ["Sales", 380], ["Customer Success", 250], ["Marketing", 150],
  ["Finance", 120], ["People", 90], ["Operations", 162], ["Legal", 8],
];

describe("suppression rules", () => {
  it("primary plus complementary: Legal (8) and the next smallest department are hidden", () => {
    const cs = cells(["department"], CLOSING_BY_DEPT.map(([d, n]) => [{ department: d }, n]));
    const out = suppressCells(cs, { dims: ["department"], k: K });
    expect(out.counts).toEqual({ primary: 1, complementary: 1, hidden_sum: 0, complement: 0 });
    const hidden = out.rows.filter((r) => r.suppressed).map((r) => r.dims["department"]);
    expect(hidden.sort()).toEqual(["Legal", "People"]);
    for (const r of out.rows) {
      if (r.suppressed) expect(r.values["headcount"]).toBeNull();
      expect(Object.keys(r)).not.toContain("cohort");
    }
  });

  it("hidden sum: two small cells (3 and 4) drag a third so 7 is not recoverable", () => {
    const cs = cells(["location"], [[{ location: "A" }, 3], [{ location: "B" }, 4], [{ location: "C" }, 50], [{ location: "D" }, 60]]);
    const out = suppressCells(cs, { dims: ["location"], k: K });
    expect(out.counts).toEqual({ primary: 2, complementary: 0, hidden_sum: 1, complement: 0 });
    expect(out.rows.find((r) => r.dims["location"] === "C")?.suppressed).toBe(true);
    expect(out.rows.find((r) => r.dims["location"] === "D")?.suppressed).toBe(false);
  });

  it("partitions are per time bucket and per other dimension values", () => {
    const cs = cells(["department", "location"], [
      [{ department: "Legal", location: "London" }, 2, "2025-01-01"],
      [{ department: "Sales", location: "London" }, 40, "2025-01-01"],
      [{ department: "Legal", location: "Austin" }, 30, "2025-01-01"],
      [{ department: "Sales", location: "Austin" }, 35, "2025-01-01"],
      [{ department: "Legal", location: "London" }, 20, "2025-04-01"],
      [{ department: "Sales", location: "London" }, 25, "2025-04-01"],
    ]);
    const out = suppressCells(cs, { dims: ["department", "location"], k: K });
    const s = (d: string, l: string, t: string) =>
      out.rows.find((r) => r.dims["department"] === d && r.dims["location"] === l && r.period === t)?.suppressed;
    expect(s("Legal", "London", "2025-01-01")).toBe(true);
    expect(s("Sales", "London", "2025-01-01")).toBe(true); // complementary across department
    expect(s("Legal", "Austin", "2025-01-01")).toBe(true); // complementary across location
    expect(s("Sales", "Austin", "2025-01-01")).toBe(true); // then Austin and Sales partitions need a second cell
    expect(s("Legal", "London", "2025-04-01")).toBe(false);
    expect(s("Sales", "London", "2025-04-01")).toBe(false);
  });

  it("totals are subject only to primary and complement rules", () => {
    const small = suppressCells(cells([], [[{}, 8]]), { dims: [], k: K });
    expect(small.rows[0]?.suppressed).toBe(true);
    const big = cells([], [[{}, 1780]]);
    const forced = suppressCells(big, { dims: [], k: K, forced: new Set([big[0]!.key]) });
    expect(forced.counts.complement).toBe(1);
    expect(forced.rows[0]?.values["headcount"]).toBeNull();
    expect(suppressCells(big, { dims: [], k: K }).rows[0]?.suppressed).toBe(false);
  });

  it("a partition of one row needs no complementary cell", () => {
    const out = suppressCells(cells(["department"], [[{ department: "Legal" }, 8]]), { dims: ["department"], k: K });
    expect(out.counts).toEqual({ primary: 1, complementary: 0, hidden_sum: 0, complement: 0 });
  });

  it("suppress() reads warehouse columns and drops empty-aggregate rows", () => {
    const rs = {
      columns: ["workforce__department", "headcount"],
      rows: [["Legal", 8], ["Sales", 380], ["People", 90]],
    };
    const out = suppress(rs, {
      dims: ["department"],
      dimColumns: { department: "workforce__department" },
      measures: ["headcount"],
      cohort: "headcount",
      k: K,
    });
    expect(out.rows.filter((r) => r.suppressed)).toHaveLength(2);
    const empty = toCells(
      { columns: ["headcount"], rows: [[null]] },
      { dimensions: {}, time: null, values: ["headcount"], cohort: "headcount" },
      [],
    );
    expect(empty).toEqual([]);
  });

  it("fractional cohorts (turnover average headcount) use the same threshold", () => {
    const cs = cells(["department"], [[{ department: "A" }, 9.5], [{ department: "B" }, 10.0], [{ department: "C" }, 30]]);
    const out = suppressCells(cs, { dims: ["department"], k: K });
    expect(out.rows.find((r) => r.dims["department"] === "A")?.suppressed).toBe(true);
    expect(out.rows.find((r) => r.dims["department"] === "B")?.suppressed).toBe(true);
    expect(out.rows.find((r) => r.dims["department"] === "C")?.suppressed).toBe(false);
  });
});
