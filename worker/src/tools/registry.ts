// Tools the server can invoke. Every tool is read-only, and none accepts SQL:
// run_governed_query takes a QueryIntent that the policy engine validates and
// maps to a compiled catalog entry. The planner picks an action; the server
// decides which tool runs, and runs it only through invokeTool, which refuses any
// tool that is not in TOOLS or not declared read-only. The routes that reach them:
// POST /api/ask (run_governed_query for a query, ask_clarification for a
// clarification), GET /api/catalog (list_metrics), GET /api/catalog/metrics/:metric
// (describe_metric) and GET /api/catalog/dimensions/:dimension/values
// (list_dimension_values). Inputs are checked before the call: route parameters
// against the tool's input schema, a QueryIntent by the policy engine inside
// run_governed_query (a malformed intent gets a refusal code, not a schema error).
import { z } from "zod";
import { POLICY, roleDimensions } from "../policy/policy";
import { ApiError, DIMENSION_IDS, GRAINS, METRIC_IDS, type Principal, type QueryIntent } from "../policy/types";
import { DIMENSION_VALUES } from "../policy/validate";

export interface ToolCtx {
  principal: Principal;
  /** The governed pipeline behind run_governed_query (validation, catalog entry, cost
   * guard, execution, suppression); /api/ask wires it. */
  runQuery?: (intent: QueryIntent) => Promise<unknown>;
}

export interface Tool<I> {
  name: string;
  readOnly: true;
  description: string;
  inputSchema: z.ZodType<I>;
  run(input: I, ctx: ToolCtx): Promise<unknown>;
}

const intentSchema = z.object({
  metric: z.enum(METRIC_IDS),
  groupBy: z.array(z.enum(DIMENSION_IDS)).max(2),
  grain: z.enum(GRAINS),
  timeRange: z.object({ start: z.string(), end: z.string() }),
  filters: z.array(z.object({ dimension: z.enum(DIMENSION_IDS), values: z.array(z.string()) })),
});

export const listMetrics: Tool<Record<string, never>> = {
  name: "list_metrics",
  readOnly: true,
  description: "Governed metrics and approved dimensions available to the caller's role.",
  inputSchema: z.object({}).strict() as unknown as z.ZodType<Record<string, never>>,
  async run(_input, ctx) {
    const role = POLICY.roles[ctx.principal.role];
    return {
      metrics: role.metrics.map((id) => ({
        id,
        label: POLICY.metrics[id].label,
        definition: POLICY.metrics[id].definition,
        unit: POLICY.metrics[id].unit,
        dimensions: roleDimensions(POLICY, ctx.principal.role, id),
      })),
      grains: role.grains,
    };
  },
};

export const describeMetric: Tool<{ metric: (typeof METRIC_IDS)[number] }> = {
  name: "describe_metric",
  readOnly: true,
  description: "Definition, unit and approved dimensions of one governed metric.",
  inputSchema: z.object({ metric: z.enum(METRIC_IDS) }),
  async run(input, ctx) {
    const role = POLICY.roles[ctx.principal.role];
    if (!role.metrics.includes(input.metric)) {
      throw new ApiError(403, "METRIC_NOT_ALLOWED", "That metric is not available to your role.", role.metrics);
    }
    const m = POLICY.metrics[input.metric];
    return { id: m.id, label: m.label, definition: m.definition, unit: m.unit, dimensions: roleDimensions(POLICY, ctx.principal.role, m.id) };
  },
};

export const listDimensionValues: Tool<{ dimension: (typeof DIMENSION_IDS)[number] }> = {
  name: "list_dimension_values",
  readOnly: true,
  description: "Approved labels of one dimension (non-PII category labels only).",
  inputSchema: z.object({ dimension: z.enum(DIMENSION_IDS) }),
  async run(input, ctx) {
    const role = POLICY.roles[ctx.principal.role];
    if (!role.dimensions.includes(input.dimension)) return { dimension: input.dimension, values: [] };
    const scope = ctx.principal.scope.departments;
    const values = DIMENSION_VALUES[input.dimension].filter((v) => input.dimension !== "department" || scope === "all" || scope.includes(v));
    return { dimension: input.dimension, values };
  },
};

export const runGovernedQuery: Tool<QueryIntent> = {
  name: "run_governed_query",
  readOnly: true,
  description: "Validate a query intent against the caller's policy and run its compiled MetricFlow catalog entry.",
  inputSchema: intentSchema as unknown as z.ZodType<QueryIntent>,
  async run(input, ctx) {
    if (!ctx.runQuery) throw new Error("runQuery is not wired");
    return ctx.runQuery(input);
  },
};

export const askClarification: Tool<{ question: string; options: { id: string; label: string }[] }> = {
  name: "ask_clarification",
  readOnly: true,
  description: "Return a clarification question with selectable options to the user.",
  inputSchema: z.object({
    question: z.string().max(400),
    options: z.array(z.object({ id: z.string().max(10), label: z.string().max(120) })).max(6),
  }),
  async run(input) {
    return { type: "clarification", question: input.question, options: input.options };
  },
};

export const TOOLS = [listMetrics, describeMetric, listDimensionValues, runGovernedQuery, askClarification] as const;

/** How the server runs a tool. Tests pass a recording invoker that delegates to invokeTool. */
export type ToolInvoker = <I>(tool: Tool<I>, input: I, ctx: ToolCtx) => Promise<unknown>;

/** The only way the server runs a tool: it must be one of TOOLS and declared read-only. */
export const invokeTool: ToolInvoker = async (tool, input, ctx) => {
  if (!(TOOLS as readonly unknown[]).includes(tool) || tool.readOnly !== true) {
    throw new ApiError(500, "INTERNAL", `refusing to run ${String(tool.name)}: not a registered read-only tool`);
  }
  return tool.run(input, ctx);
};
