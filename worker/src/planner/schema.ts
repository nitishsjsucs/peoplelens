// The planner's output contract: a flat JSON object (easy for small models and
// JSON-mode decoders), mapped by the server into a PlannerOutput. Enums keep
// unapproved dimensions out of the decoder's reach; the server validates anyway.
import { z } from "zod";
import {
  DIMENSION_IDS,
  type DimensionId,
  GRAINS,
  type Grain,
  METRIC_IDS,
  PLANNER_REFUSAL_CODES,
  type PlannerRefusalCode,
  type QueryIntent,
} from "../policy/types";

export const PLANNER_JSON_SCHEMA: Record<string, unknown> = {
  type: "object",
  additionalProperties: false,
  properties: {
    action: { type: "string", enum: ["query", "clarify", "refuse"] },
    metric: { type: "string", enum: [...METRIC_IDS, ""] },
    group_by: { type: "array", items: { type: "string", enum: [...DIMENSION_IDS] }, maxItems: 3 },
    grain: { type: "string", enum: [...GRAINS] },
    start: { type: "string" },
    end: { type: "string" },
    filters: {
      type: "array",
      maxItems: 4,
      items: {
        type: "object",
        additionalProperties: false,
        properties: {
          dimension: { type: "string", enum: [...DIMENSION_IDS] },
          values: { type: "array", items: { type: "string" }, maxItems: 8 },
        },
        required: ["dimension", "values"],
      },
    },
    clarification_question: { type: "string" },
    clarification_options: {
      type: "array",
      maxItems: 4,
      items: {
        type: "object",
        additionalProperties: false,
        properties: {
          label: { type: "string" },
          metric: { type: "string", enum: [...METRIC_IDS, ""] },
        },
        required: ["label", "metric"],
      },
    },
    refusal_reason: { type: "string", enum: [...PLANNER_REFUSAL_CODES, ""] },
  },
  required: [
    "action",
    "metric",
    "group_by",
    "grain",
    "start",
    "end",
    "filters",
    "clarification_question",
    "clarification_options",
    "refusal_reason",
  ],
};

const flat = z.object({
  action: z.enum(["query", "clarify", "refuse"]),
  metric: z.union([z.enum(METRIC_IDS), z.literal("")]).default(""),
  group_by: z.array(z.string()).max(8).default([]),
  grain: z.enum(GRAINS).default("none"),
  start: z.string().max(40).default(""),
  end: z.string().max(40).default(""),
  filters: z
    .array(z.object({ dimension: z.string().max(40), values: z.array(z.string().max(80)).max(20) }))
    .max(10)
    .default([]),
  clarification_question: z.string().max(400).default(""),
  clarification_options: z
    .array(z.object({ label: z.string().max(120), metric: z.union([z.enum(METRIC_IDS), z.literal("")]).default("") }))
    .max(8)
    .default([]),
  refusal_reason: z.string().max(60).default(""),
});

export type FlatPlannerOutput = z.infer<typeof flat>;

export interface ClarifyOptionOut {
  id: string;
  label: string;
  patch: Partial<QueryIntent>;
}

/** A partial intent: the server fills in what the planner left empty. */
export interface DraftIntent {
  metric: QueryIntent["metric"];
  groupBy: DimensionId[];
  grain: Grain;
  start: string;
  end: string;
  filters: { dimension: DimensionId; values: string[] }[];
}

export type PlannerOutput =
  | { action: "query"; intent: DraftIntent }
  | { action: "clarify"; question: string; options: ClarifyOptionOut[]; draft: Partial<DraftIntent> }
  | { action: "refuse"; reason: PlannerRefusalCode };

/** Parse raw model output (object or JSON text). Returns null when it is not a usable plan. */
export function parsePlannerOutput(raw: unknown): PlannerOutput | null {
  let value = raw;
  if (typeof value === "string") {
    const text = value.trim().replace(/^```(?:json)?\s*/i, "").replace(/```$/, "");
    try {
      value = JSON.parse(text);
    } catch {
      return null;
    }
  }
  const parsed = flat.safeParse(value);
  if (!parsed.success) return null;
  const f = parsed.data;
  // Unknown dimension names are kept (as strings) so the validator can refuse them with a clear code.
  const groupBy = f.group_by as DimensionId[];
  const filters = f.filters.map((x) => ({ dimension: x.dimension as DimensionId, values: x.values }));
  if (f.action === "query") {
    if (f.metric === "") return null;
    return { action: "query", intent: { metric: f.metric, groupBy, grain: f.grain, start: f.start, end: f.end, filters } };
  }
  if (f.action === "clarify") {
    const options = f.clarification_options
      .filter((o) => o.label.trim() !== "")
      .map((o, i) => ({ id: `o${i + 1}`, label: o.label.trim(), patch: o.metric ? { metric: o.metric } : {} }));
    if (f.clarification_question.trim() === "" || options.length === 0) return null;
    const draft: Partial<DraftIntent> = { groupBy, grain: f.grain, start: f.start, end: f.end, filters };
    if (f.metric) draft.metric = f.metric;
    return { action: "clarify", question: f.clarification_question.trim(), options, draft };
  }
  const reason = (PLANNER_REFUSAL_CODES as readonly string[]).includes(f.refusal_reason)
    ? (f.refusal_reason as PlannerRefusalCode)
    : "OUT_OF_DOMAIN";
  return { action: "refuse", reason };
}
