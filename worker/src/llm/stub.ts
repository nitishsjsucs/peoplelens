// Deterministic keyword planner for CI and tests. It was written only against
// the dev question templates (eval/templates/dev.yaml), never the eval set, and
// its results are never reported as model accuracy.
import { POLICY } from "../policy/policy";
import type { DimensionId, MetricId } from "../policy/types";
import { grainHint } from "../planner/timephrases";
import { findBreakdowns, findMetricCues } from "../planner/values";
import type { LlmClient, PlanCall, PlannerInput } from "./types";

const FOLLOW_UP = /^\s*(and|what about|how about|now|same|instead|ok|okay|also|then|just|only|break (it|that) down|split (it|that))\b/i;

export class StubPlanner implements LlmClient {
  readonly provider = "stub" as const;
  readonly model = "stub-keyword-planner";

  async plan(input: PlannerInput): Promise<PlanCall> {
    const q = input.question;
    const cues = findMetricCues(q);
    const previous = input.previous;
    const followUp = previous !== null && (cues.length === 0 || FOLLOW_UP.test(q));
    const breakdowns = findBreakdowns(q);
    const grain = grainHint(q);

    let metric: MetricId | "" = cues[0] ?? "";
    if (followUp && cues.length === 0 && previous) metric = previous.metric;
    if (metric === "") {
      const options = input.allowedMetrics.slice(0, 3).map((m) => ({ label: POLICY.metrics[m as MetricId].label, metric: m }));
      return {
        ms: 0,
        raw: {
          action: "clarify",
          metric: "",
          group_by: breakdowns,
          grain: grain ?? "none",
          start: "",
          end: "",
          filters: this.filters(input, []),
          clarification_question: "Which metric would you like?",
          clarification_options: options,
          refusal_reason: "",
        },
      };
    }
    let groupBy: DimensionId[] = breakdowns;
    let filters = this.filters(input, breakdowns);
    if (followUp && previous) {
      if (groupBy.length === 0) groupBy = previous.groupBy;
      if (filters.length === 0 && input.values.length === 0) filters = previous.filters;
    }
    const start = input.timeHint?.start ?? (followUp && previous ? previous.timeRange.start : "");
    const end = input.timeHint?.end ?? (followUp && previous ? previous.timeRange.end : "");
    return {
      ms: 0,
      raw: {
        action: "query",
        metric,
        group_by: groupBy,
        grain: grain ?? (followUp && previous ? previous.grain : "none"),
        start,
        end,
        filters,
        clarification_question: "",
        clarification_options: [],
        refusal_reason: "",
      },
    };
  }

  private filters(input: PlannerInput, breakdowns: DimensionId[]): { dimension: DimensionId; values: string[] }[] {
    const byDim = new Map<DimensionId, string[]>();
    for (const v of input.values) {
      // A single value of a breakdown dimension is a filter; several values of it are a comparison.
      byDim.set(v.dimension, [...(byDim.get(v.dimension) ?? []), v.value]);
    }
    const out: { dimension: DimensionId; values: string[] }[] = [];
    for (const [dimension, values] of byDim) {
      if (breakdowns.includes(dimension) && values.length < 2) continue;
      out.push({ dimension, values });
    }
    return out;
  }
}
