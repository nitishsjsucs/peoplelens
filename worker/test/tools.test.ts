import { z } from "zod";
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/policy/types";
import { TOOLS, type Tool, describeMetric, invokeTool, listDimensionValues, listMetrics, runGovernedQuery } from "../src/tools/registry";
import { EXEC, HRBP, RECRUITER } from "./helpers/principals";

function propertyNames(schema: unknown, out: string[] = []): string[] {
  if (!schema || typeof schema !== "object") return out;
  const s = schema as Record<string, unknown>;
  if (s["properties"] && typeof s["properties"] === "object") {
    for (const [name, child] of Object.entries(s["properties"] as Record<string, unknown>)) {
      out.push(name);
      propertyNames(child, out);
    }
  }
  for (const key of ["items", "anyOf", "oneOf", "allOf", "additionalProperties"]) {
    const v = s[key];
    if (Array.isArray(v)) v.forEach((x) => propertyNames(x, out));
    else if (v && typeof v === "object") propertyNames(v, out);
  }
  return out;
}

describe("tool registry", () => {
  it("has the five spec tools, all read-only, none accepting SQL", () => {
    expect(TOOLS.map((t) => t.name)).toEqual([
      "list_metrics",
      "describe_metric",
      "list_dimension_values",
      "run_governed_query",
      "ask_clarification",
    ]);
    for (const tool of TOOLS) {
      expect(tool.readOnly).toBe(true);
      const names = propertyNames(z.toJSONSchema(tool.inputSchema as z.ZodType));
      for (const n of names) {
        expect(n.toLowerCase(), `${tool.name}.${n}`).not.toContain("sql");
        expect(n.toLowerCase(), `${tool.name}.${n}`).not.toContain("query_text");
      }
    }
  });

  it("filters the catalog and values by role and scope", async () => {
    const exec = (await listMetrics.run({}, { principal: EXEC })) as { metrics: { id: string; dimensions: string[] }[] };
    expect(exec.metrics.find((m) => m.id === "headcount")?.dimensions).toEqual(["department", "location"]);
    const values = (await listDimensionValues.run({ dimension: "department" }, { principal: HRBP })) as { values: string[] };
    expect(values.values).toEqual(["Engineering", "Sales"]);
    const none = (await listDimensionValues.run({ dimension: "employment_type" }, { principal: EXEC })) as { values: string[] };
    expect(none.values).toEqual([]);
  });

  it("invokeTool runs only registered read-only tools", async () => {
    const listed = (await invokeTool(listMetrics, {}, { principal: EXEC })) as { metrics: unknown[] };
    expect(listed.metrics).toHaveLength(6);
    // a look-alike that is not the registered object, and a copy that drops readOnly, are refused
    const lookalike: Tool<Record<string, never>> = { ...listMetrics };
    const writable = { ...listMetrics, readOnly: false } as unknown as Tool<Record<string, never>>;
    for (const tool of [lookalike, writable]) {
      await expect(invokeTool(tool, {}, { principal: EXEC })).rejects.toMatchObject({ code: "INTERNAL" });
    }
    // run_governed_query has no implementation of its own: only the /api/ask pipeline wires one
    await expect(
      invokeTool(runGovernedQuery, { metric: "headcount", groupBy: [], grain: "none", timeRange: { start: "2025-12-01", end: "2025-12-31" }, filters: [] }, { principal: EXEC }),
    ).rejects.toThrow("runQuery is not wired");
  });

  it("describe_metric refuses a metric outside the caller's role", async () => {
    const turnover = (await invokeTool(describeMetric, { metric: "turnover_rate" }, { principal: HRBP })) as { id: string; dimensions: string[] };
    expect(turnover).toMatchObject({ id: "turnover_rate", dimensions: ["department", "location", "job_level", "tenure_band"] });
    const err = await invokeTool(describeMetric, { metric: "compensation_summary" }, { principal: RECRUITER }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 403, code: "METRIC_NOT_ALLOWED" });
  });
});
