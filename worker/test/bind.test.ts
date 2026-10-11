import { describe, expect, it } from "vitest";
import { ALL, PROJECT_PLACEHOLDER, bindQuery, widen } from "../src/catalog/bind";
import { type CatalogFile, catalogKey } from "../src/catalog/catalog";
import bqJson from "../src/generated/catalog.bigquery.json";
import duckJson from "../src/generated/catalog.duckdb.json";
import { type QueryIntent } from "../src/policy/types";
import { type Validated, validateIntent } from "../src/policy/validate";
import { ANALYST, HRBP } from "./helpers/principals";
import type { Principal } from "../src/policy/types";

const duck = duckJson as unknown as CatalogFile;
const bq = bqJson as unknown as CatalogFile;

function validated(p: Principal, i: Partial<QueryIntent>): Validated {
  const r = validateIntent(
    { metric: "headcount", groupBy: [], grain: "none", timeRange: { start: "2025-12-01", end: "2025-12-31" }, filters: [], ...i },
    p,
  );
  if (!r.ok) throw new Error(r.violation.message);
  return r.value;
}

describe("bindQuery", () => {
  it("binds DuckDB parameters with sentinels, never NULL", () => {
    const v = validated(ANALYST, { filters: [{ dimension: "location", values: ["London"] }] });
    const q = bindQuery(duck.entries[catalogKey(v)]!, v, "duckdb");
    expect(q.params["start_ts"]).toEqual({ type: "DATE", value: "2025-12-01" });
    expect(q.params["f_location"]).toEqual({ type: "ARRAY<STRING>", value: ["London"] });
    expect(q.params["f_department"]).toEqual({ type: "ARRAY<STRING>", value: [ALL] });
    expect(q.params["scope_departments"]?.value).toHaveLength(8);
    expect(q.sql).toBe(duck.entries["headcount||none"]!.sql);
    for (const p of Object.values(q.params)) expect(p.value).not.toBeNull();
  });

  it("binds the HRBP scope as a parameter, not as SQL", () => {
    const v = validated(HRBP, { groupBy: ["department"] });
    const q = bindQuery(duck.entries[catalogKey(v)]!, v, "duckdb");
    expect(q.params["scope_departments"]).toEqual({ type: "ARRAY<STRING>", value: ["Engineering", "Sales"] });
    expect(q.sql).not.toContain("Engineering");
  });

  it("binds BigQuery TIMESTAMP parameters and swaps only the project placeholder", () => {
    const v = validated(ANALYST, { metric: "turnover_rate", groupBy: ["department"], grain: "quarter", timeRange: { start: "2024-01-01", end: "2024-12-31" } });
    const entry = bq.entries[catalogKey(v)]!;
    const q = bindQuery(entry, v, "bigquery", "peoplelens-abc123");
    expect(q.params["end_ts"]).toEqual({ type: "TIMESTAMP", value: "2024-12-31 00:00:00+00:00" });
    expect(q.sql).not.toContain(PROJECT_PLACEHOLDER);
    expect(q.sql).toContain("`peoplelens-abc123`.`peoplelens_semantic`.`sem_workforce_monthly`");
    expect(q.sql.replaceAll("`peoplelens-abc123`", PROJECT_PLACEHOLDER)).toBe(entry.sql);
    expect(q.sha256).toBe(entry.sha256);
    expect(() => bindQuery(entry, v, "bigquery", "Bad_Project")).toThrow();
    expect(() => bindQuery(entry, v, "bigquery")).toThrow();
  });

  it("matches the parameter contract of every catalog entry in both dialects", () => {
    for (const cat of [duck, bq]) {
      for (const e of Object.values(cat.entries)) {
        const range = e.metric === "headcount" || e.metric === "fte" || e.metric === "compensation_summary"
          ? { start: "2025-12-01", end: "2025-12-31" }
          : { start: "2025-01-01", end: "2025-12-31" };
        const v = validated(ANALYST, { metric: e.metric, groupBy: e.group_by, grain: e.grain, timeRange: range });
        const q = bindQuery(e, v, cat.header.dialect, "peoplelens-abc123");
        expect(Object.keys(q.params).sort()).toEqual(e.params.map((p) => p.name).sort());
      }
    }
  });

  it("refuses an entry that does not match the intent", () => {
    const v = validated(ANALYST, {});
    expect(() => bindQuery(duck.entries["fte||none"]!, v, "duckdb")).toThrow();
  });

  it("widen drops filters and the scope for the widened dimensions", () => {
    const v = validated(HRBP, { filters: [{ dimension: "location", values: ["London"] }] });
    expect(v.restricted).toEqual(["department", "location"]);
    const w = widen(v, ["department"]);
    expect(w.scopeDepartments).toBe("all");
    expect(w.filters.location).toEqual(["London"]);
    expect(w.restricted).toEqual(["location"]);
    const both = widen(v, ["department", "location"]);
    expect(both.filters).toEqual({});
  });
});
