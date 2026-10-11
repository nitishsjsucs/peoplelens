import { describe, expect, it } from "vitest";
import { bindQuery } from "../src/catalog/bind";
import { type CatalogFile, catalogKey } from "../src/catalog/catalog";
import bqJson from "../src/generated/catalog.bigquery.json";
import { POLICY } from "../src/policy/policy";
import { validateIntent } from "../src/policy/validate";
import { BigQueryWarehouse, decodeValue, toQueryParameters } from "../src/warehouse/bigquery";
import type { GoogleTokenSource } from "../src/warehouse/googleAuth";
import { EXEC } from "./helpers/principals";

const bq = bqJson as unknown as CatalogFile;
const tokens = { token: async () => "ya29.test" } as unknown as GoogleTokenSource;

function bound() {
  const r = validateIntent(
    {
      metric: "headcount",
      groupBy: ["department"],
      grain: "quarter",
      timeRange: { start: "2025-01-01", end: "2025-12-31" },
      filters: [{ dimension: "location", values: ["London"] }],
    },
    EXEC,
  );
  if (!r.ok) throw new Error("invalid");
  return bindQuery(bq.entries[catalogKey(r.value)]!, r.value, "bigquery", "peoplelens-abc123");
}

interface Captured {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: Record<string, unknown> | null;
}

function recorder(responses: unknown[]) {
  const calls: Captured[] = [];
  const fetcher: typeof fetch = async (input, init) => {
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((v, k) => (headers[k] = v));
    calls.push({
      url: String(input),
      method: init?.method ?? "GET",
      headers,
      body: init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null,
    });
    const next = responses.shift();
    if (next instanceof Response) return next;
    return Response.json(next ?? {});
  };
  return { calls, fetcher };
}

const SCHEMA = {
  fields: [
    { name: "offer__department", type: "STRING" },
    { name: "metric_time__quarter", type: "TIMESTAMP" },
    { name: "headcount", type: "INTEGER" },
  ],
};

describe("BigQueryWarehouse", () => {
  it("every job carries maximumBytesBilled equal to the role cap, typed params and labels", async () => {
    const { calls, fetcher } = recorder([
      {
        jobComplete: true,
        schema: SCHEMA,
        rows: [{ f: [{ v: "Sales" }, { v: "1735689600000000" }, { v: "42" }] }],
        totalBytesBilled: "10485760",
      },
    ]);
    const wh = new BigQueryWarehouse({ projectId: "peoplelens-abc123", tokens, fetcher });
    const cap = POLICY.limits.per_query_byte_cap.executive;
    const res = await wh.run(bound(), { maxBytesBilled: cap, timeoutMs: 8000, labels: { app: "peoplelens", role: "executive" } });
    const call = calls[0]!;
    expect(call.url).toBe("https://bigquery.googleapis.com/bigquery/v2/projects/peoplelens-abc123/queries");
    expect(call.headers["authorization"]).toBe("Bearer ya29.test");
    expect(call.body).toMatchObject({
      useLegacySql: false,
      parameterMode: "NAMED",
      location: "US",
      maximumBytesBilled: String(cap),
      timeoutMs: 8000,
      jobTimeoutMs: "15000",
      useQueryCache: true,
      labels: { app: "peoplelens", role: "executive" },
    });
    expect(call.body?.["dryRun"]).toBeUndefined();
    const params = call.body?.["queryParameters"] as { name: string }[];
    expect(params.find((p) => p.name === "f_location")).toEqual({
      name: "f_location",
      parameterType: { type: "ARRAY", arrayType: { type: "STRING" } },
      parameterValue: { arrayValues: [{ value: "London" }] },
    });
    expect(params.find((p) => p.name === "start_ts")).toEqual({
      name: "start_ts",
      parameterType: { type: "TIMESTAMP" },
      parameterValue: { value: "2025-01-01 00:00:00+00:00" },
    });
    expect(res.rows).toEqual([["Sales", "2025-01-01", 42]]);
    expect(res.billedBytes).toBe(10485760);
  });

  it("dry runs read totalBytesProcessed and are cached per catalog key", async () => {
    const { calls, fetcher } = recorder([{ totalBytesProcessed: "123456" }]);
    const wh = new BigQueryWarehouse({ projectId: "peoplelens-abc123", tokens, fetcher });
    expect(await wh.dryRun(bound())).toEqual({ estimatedBytes: 123456 });
    expect(await wh.dryRun(bound())).toEqual({ estimatedBytes: 123456 });
    expect(calls).toHaveLength(1);
    expect(calls[0]?.body?.["dryRun"]).toBe(true);
  });

  it("polls getQueryResults while the job is incomplete", async () => {
    const { calls, fetcher } = recorder([
      { jobComplete: false, jobReference: { projectId: "peoplelens-abc123", jobId: "job_1", location: "US" } },
      { jobComplete: true, schema: SCHEMA, rows: [] },
    ]);
    const wh = new BigQueryWarehouse({ projectId: "peoplelens-abc123", tokens, fetcher });
    const res = await wh.run(bound(), { maxBytesBilled: 1, timeoutMs: 8000, labels: {} });
    expect(res.rows).toEqual([]);
    expect(calls[1]?.url).toContain("/projects/peoplelens-abc123/queries/job_1?");
    expect(calls[1]?.method).toBe("GET");
  });

  it("maps BigQuery's bytes-billed refusal to COST_LIMIT_EXCEEDED", async () => {
    const { fetcher } = recorder([
      Response.json(
        { error: { code: 400, message: "Query exceeded limit for bytes billed: 1048576. 10485760 or higher required." } },
        { status: 400 },
      ),
    ]);
    const wh = new BigQueryWarehouse({ projectId: "peoplelens-abc123", tokens, fetcher });
    await expect(wh.run(bound(), { maxBytesBilled: 1048576, timeoutMs: 8000, labels: {} })).rejects.toMatchObject({
      code: "COST_LIMIT_EXCEEDED",
    });
  });

  it("never sends SQL other than the bound catalog SQL", async () => {
    const { calls, fetcher } = recorder([{ jobComplete: true, schema: SCHEMA, rows: [] }]);
    const wh = new BigQueryWarehouse({ projectId: "peoplelens-abc123", tokens, fetcher });
    const q = bound();
    await wh.run(q, { maxBytesBilled: 1, timeoutMs: 8000, labels: {} });
    expect(calls[0]?.body?.["query"]).toBe(q.sql);
  });

  it("decodes BigQuery value formats", () => {
    expect(decodeValue("TIMESTAMP", "1.7356896E9")).toBe("2025-01-01");
    expect(decodeValue("TIMESTAMP", "1735689600000000")).toBe("2025-01-01");
    expect(decodeValue("FLOAT", "0.125")).toBe(0.125);
    expect(decodeValue("STRING", null)).toBeNull();
    expect(toQueryParameters({ x: { type: "DATE", value: "2025-01-01" } })).toEqual([
      { name: "x", parameterType: { type: "DATE" }, parameterValue: { value: "2025-01-01" } },
    ]);
  });
});
