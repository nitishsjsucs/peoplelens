import { beforeAll, describe, expect, it } from "vitest";
import { roundForDisclosure, roundTo } from "../src/guard/disclosure";
import type { OutputRow } from "../src/guard/suppression";
import { POLICY } from "../src/policy/policy";
import type { BoundQuery } from "../src/catalog/bind";
import type { RunOptions, RunResult } from "../src/warehouse/types";
import { harness } from "./helpers/app";
import { FakeWarehouse } from "./helpers/fakeWarehouse";
import { ROLE_EMAILS, type TestKeys, makeKeys } from "./helpers/keys";

let keys: TestKeys;
beforeAll(async () => {
  keys = await makeKeys();
});

type Json = Record<string, any>;

/** Salaries with cents, so any value that leaves the Worker unrounded is visible. */
class SalaryWarehouse extends FakeWarehouse {
  override async run(q: BoundQuery, o: RunOptions): Promise<RunResult> {
    const r = await super.run(q, o);
    const avg = r.columns.indexOf("avg_base_salary");
    const median = r.columns.indexOf("median_base_salary");
    const rows = r.rows.map((row, i) => row.map((v, c) => (c === avg ? 123_456.78 + i : c === median ? 98_765.4 + i : v)));
    return { ...r, rows };
  }
}

function row(values: Record<string, number | null>): OutputRow {
  return { dims: {}, period: null, values, suppressed: false };
}

describe("disclosure rounding", () => {
  it("rounds half up to the step, only for the measures the policy lists", () => {
    expect(roundTo(151_234.56, 1000)).toBe(151_000);
    expect(roundTo(151_500, 1000)).toBe(152_000);
    expect(roundTo(151_499.99, 1000)).toBe(151_000);
    const comp = POLICY.metrics.compensation_summary;
    expect(comp.disclosure_round).toEqual({ avg_base_salary: 1000, median_base_salary: 1000 });
    const [r] = roundForDisclosure([row({ avg_base_salary: 274_300.4, median_base_salary: null, cohort_employees: 53 })], comp);
    expect(r?.values).toEqual({ avg_base_salary: 274_000, median_base_salary: null, cohort_employees: 53 });
    for (const id of ["headcount", "fte", "turnover_rate", "offer_acceptance_rate", "time_to_fill_days"] as const) {
      expect(POLICY.metrics[id].disclosure_round).toBeUndefined();
      expect(roundForDisclosure([row({ x: 1.2345 })], POLICY.metrics[id])[0]?.values).toEqual({ x: 1.2345 });
    }
  });

  it("no compensation value leaves /api/ask unrounded, in the rows or the narrative", async () => {
    const h = harness(keys, { warehouse: new SalaryWarehouse() });
    const a = ROLE_EMAILS.people_analytics;
    const r = (await h.ask(a, await h.session(a), "average salary by location")) as Json;
    expect(r.type).toBe("answer");
    expect(r.answer.metric).toBe("compensation_summary");
    expect(r.answer.columns.filter((c: Json) => c.kind === "measure")).toEqual([
      { name: "avg_base_salary", kind: "measure", unit: "USD", rounded_to: 1000 },
      { name: "median_base_salary", kind: "measure", unit: "USD", rounded_to: 1000 },
    ]);
    const shown = (r.answer.rows as Json[]).filter((x) => !x.suppressed);
    expect(shown.length).toBeGreaterThan(0);
    for (const x of shown) {
      expect(x.avg_base_salary % 1000).toBe(0);
      expect(x.median_base_salary % 1000).toBe(0);
    }
    expect(r.answer.narrative).toContain("$123,000");
    expect(r.answer.narrative).toContain("Values are rounded to the nearest $1,000.");
    expect(JSON.stringify(r)).not.toMatch(/123,?45[67]|98,?765/);
  });
});
