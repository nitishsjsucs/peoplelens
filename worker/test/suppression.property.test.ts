// Property tests (fast-check) for the suppression invariants in SPEC.md 9.5.
import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { applyComplementRule } from "../src/guard/complement";
import { type Cell, cellKey, suppressCells } from "../src/guard/suppression";

const K = 10;
const DIM_VALUES = { a: ["a1", "a2", "a3", "a4"], b: ["b1", "b2", "b3"] } as const;
const TIMES = ["2025-01-01", "2025-04-01", null] as const;

const resultSet = fc
  .record({
    dims: fc.constantFrom<string[]>([], ["a"], ["a", "b"]),
    timeCount: fc.integer({ min: 1, max: 2 }),
    cohorts: fc.array(fc.oneof(fc.integer({ min: 0, max: 14 }), fc.integer({ min: 0, max: 400 })), { minLength: 24, maxLength: 24 }),
    keep: fc.array(fc.boolean(), { minLength: 24, maxLength: 24 }),
  })
  .map(({ dims, timeCount, cohorts, keep }) => {
    const out: Cell[] = [];
    let i = 0;
    const times = TIMES.slice(0, timeCount);
    const aVals = dims.includes("a") ? DIM_VALUES.a : [undefined];
    const bVals = dims.includes("b") ? DIM_VALUES.b : [undefined];
    for (const t of times) {
      for (const a of aVals) {
        for (const b of bVals) {
          const idx = i++ % 24;
          if (!keep[idx] && dims.length > 0) continue;
          const d: Record<string, string> = {};
          if (a) d["a"] = a;
          if (b) d["b"] = b;
          const cohort = cohorts[idx] ?? 0;
          out.push({ key: cellKey(d, dims, t), dims: d, time: t, values: { m: cohort * 2 }, cohort });
        }
      }
    }
    return { dims, cells: out };
  });

function partitions(cells: Cell[], dims: string[]): Cell[][] {
  const out: Cell[][] = [];
  for (const d of dims) {
    const groups = new Map<string, Cell[]>();
    for (const c of cells) {
      const k = JSON.stringify([...dims.filter((x) => x !== d).map((x) => c.dims[x]), c.time]);
      groups.set(k, [...(groups.get(k) ?? []), c]);
    }
    out.push(...groups.values());
  }
  return out;
}

describe("suppression invariants", () => {
  it("no unsuppressed cell has a cohort below k", () => {
    fc.assert(
      fc.property(resultSet, ({ dims, cells }) => {
        const out = suppressCells(cells, { dims, k: K });
        for (const c of cells) if (c.cohort < K) expect(out.suppressedKeys.has(c.key)).toBe(true);
        for (const r of out.rows) if (r.suppressed) expect(Object.values(r.values).every((v) => v === null)).toBe(true);
      }),
      { numRuns: 400 },
    );
  });

  it("no partition has exactly one suppressed cell unless it has one row", () => {
    fc.assert(
      fc.property(resultSet, ({ dims, cells }) => {
        const out = suppressCells(cells, { dims, k: K });
        for (const p of partitions(cells, dims)) {
          const n = p.filter((c) => out.suppressedKeys.has(c.key)).length;
          if (p.length > 1) expect(n).not.toBe(1);
        }
      }),
      { numRuns: 400 },
    );
  });

  it("every partition's suppressed cohort sum is 0, at least k, or the whole partition", () => {
    fc.assert(
      fc.property(resultSet, ({ dims, cells }) => {
        const out = suppressCells(cells, { dims, k: K });
        for (const p of partitions(cells, dims)) {
          const hidden = p.filter((c) => out.suppressedKeys.has(c.key));
          const sum = hidden.reduce((s, c) => s + c.cohort, 0);
          expect(sum === 0 || sum >= K || hidden.length === p.length).toBe(true);
        }
      }),
      { numRuns: 400 },
    );
  });

  it("is idempotent", () => {
    fc.assert(
      fc.property(resultSet, ({ dims, cells }) => {
        const once = suppressCells(cells, { dims, k: K });
        const twice = suppressCells(cells, { dims, k: K, forced: once.suppressedKeys });
        expect([...twice.suppressedKeys].sort()).toEqual([...once.suppressedKeys].sort());
      }),
      { numRuns: 300 },
    );
  });
});

describe("complement rule invariant over a fake warehouse", () => {
  // A random population of people with (dept, loc); the main query filters
  // locations to a proper subset, the probe widens it.
  const population = fc.array(
    fc.record({ dept: fc.constantFrom("Eng", "Sales", "Legal", "Ops"), loc: fc.constantFrom("SJ", "NY", "LDN") }),
    { minLength: 0, maxLength: 300 },
  );
  const subset = fc.subarray(["SJ", "NY", "LDN"], { minLength: 1, maxLength: 2 });

  it("no unsuppressed cell differs from its widened counterpart by 1 to 9", () => {
    fc.assert(
      fc.property(population, subset, fc.boolean(), (people, locs, grouped) => {
        const dims = grouped ? ["department"] : [];
        const aggregate = (rows: typeof people): Cell[] => {
          const counts = new Map<string, number>();
          for (const r of rows) {
            const k = grouped ? r.dept : "";
            counts.set(k, (counts.get(k) ?? 0) + 1);
          }
          return [...counts.entries()].map(([dept, n]) => {
            const d: Record<string, string> = grouped ? { department: dept } : {};
            return { key: cellKey(d, dims, null), dims: d, time: null, values: { headcount: n }, cohort: n };
          });
        };
        const main = aggregate(people.filter((p) => locs.includes(p.loc)));
        const wide = aggregate(people);
        const { suppressKeys } = applyComplementRule(main, [wide], { k: K });
        const out = suppressCells(main, { dims, k: K, forced: suppressKeys });
        const wideBy = new Map(wide.map((c) => [c.key, c.cohort] as const));
        for (const c of main) {
          if (out.suppressedKeys.has(c.key)) continue;
          const diff = (wideBy.get(c.key) ?? c.cohort) - c.cohort;
          expect(diff === 0 || diff >= K).toBe(true);
        }
      }),
      { numRuns: 400 },
    );
  });
});
