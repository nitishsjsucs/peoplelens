import { z } from "zod";
import { describe, expect, it } from "vitest";
import { TOOLS, listDimensionValues, listMetrics } from "../src/tools/registry";
import { EXEC, HRBP } from "./helpers/principals";

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
});
