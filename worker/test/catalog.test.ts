import { describe, expect, it } from "vitest";
import { type CatalogFile, catalogKey, lookupEntry } from "../src/catalog/catalog";
import bqJson from "../src/generated/catalog.bigquery.json";
import duckJson from "../src/generated/catalog.duckdb.json";
import { POLICY } from "../src/policy/policy";

const duck = duckJson as unknown as CatalogFile;
const bq = bqJson as unknown as CatalogFile;

describe("compiled catalog", () => {
  it("has 312 entries per dialect with matching keys", () => {
    expect(duck.header.entries).toBe(312);
    expect(bq.header.entries).toBe(312);
    expect(Object.keys(duck.entries).sort()).toEqual(Object.keys(bq.entries).sort());
    expect(duck.header.dialect).toBe("duckdb");
    expect(bq.header.dialect).toBe("bigquery");
  });

  it("keys follow metric|sorted(group_by)|grain", () => {
    for (const [key, e] of Object.entries(duck.entries)) {
      expect(catalogKey({ metric: e.metric, groupBy: e.group_by, grain: e.grain })).toBe(key);
      expect(e.mf_metrics).toContain(POLICY.metrics[e.metric].cohort_metric);
    }
    expect(catalogKey({ metric: "turnover_rate", groupBy: ["location", "department"], grain: "quarter" })).toBe(
      "turnover_rate|department,location|quarter",
    );
  });

  it("lookup never resolves inherited object properties", () => {
    expect(lookupEntry(duck, "__proto__")).toBeUndefined();
    expect(lookupEntry(duck, "constructor")).toBeUndefined();
    expect(lookupEntry(duck, "headcount||none")?.metric).toBe("headcount");
  });
});
