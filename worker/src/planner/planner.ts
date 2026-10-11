// One planner call per question: screen, build the prompt, call the provider,
// parse, retry once on invalid output, then resolve the draft into a full intent
// with deterministic time handling. The LLM never sees results.
import type { LlmClient, PlannerInput, TokenUsage } from "../llm/types";
import { LlmError } from "../llm/types";
import { POLICY } from "../policy/policy";
import type { DimensionId, Policy, Principal, QueryIntent } from "../policy/types";
import type { PendingClarification } from "../session/userDO";
import { buildMessages } from "./prompt";
import { type DraftIntent, PLANNER_JSON_SCHEMA, type PlannerOutput, parsePlannerOutput } from "./schema";
import { type TimeHint, grainHint, rangeFor, resolveTimePhrase } from "./timephrases";
import { findValueMentions, screenRequest } from "./values";

export interface PlanContext {
  question: string;
  principal: Principal;
  previous: QueryIntent | null;
  pending: PendingClarification | null;
  llm: LlmClient;
  requestId: string;
  turn: number;
  policy?: Policy;
}

export interface PlanResult {
  output: PlannerOutput;
  intent: QueryIntent | null;
  llmMs: number;
  gatewayLogId: string | null;
  usage: TokenUsage | null;
  attempts: number;
  invalidOutput: boolean;
  screened: boolean;
  llmError: string | null;
}

const ISO = /^\d{4}-\d{2}-\d{2}$/;

export const REPHRASE_QUESTION = "Could you rephrase? For example: \"headcount by department\" or \"turnover in 2024\".";

/** Turn a planner draft into a complete intent. Time words in the question win. */
export function resolveDraft(
  draft: Partial<DraftIntent> & { metric: QueryIntent["metric"] },
  hint: TimeHint | null,
  question: string,
  fallback: { start: string; end: string } | null,
  policy: Policy = POLICY,
): QueryIntent {
  const kind = policy.metrics[draft.metric]?.kind ?? "flow";
  let range: { start: string; end: string };
  if (hint) range = rangeFor(kind, hint);
  else if (draft.start && draft.end && ISO.test(draft.start) && ISO.test(draft.end)) range = { start: draft.start, end: draft.end };
  else if (fallback) range = fallback;
  else range = rangeFor(kind, null);
  const g = grainHint(question);
  return {
    metric: draft.metric,
    groupBy: [...new Set(draft.groupBy ?? [])] as DimensionId[],
    grain: g ?? draft.grain ?? "none",
    timeRange: range,
    filters: (draft.filters ?? []).map((f) => ({ dimension: f.dimension, values: [...f.values] })),
  };
}

export async function plan(ctx: PlanContext): Promise<PlanResult> {
  const policy = ctx.policy ?? POLICY;
  const base: PlanResult = {
    output: { action: "refuse", reason: "OUT_OF_DOMAIN" },
    intent: null,
    llmMs: 0,
    gatewayLogId: null,
    usage: null,
    attempts: 0,
    invalidOutput: false,
    screened: false,
    llmError: null,
  };

  const screen = screenRequest(ctx.question);
  if (screen) return { ...base, output: { action: "refuse", reason: screen.code }, screened: true };

  const hint = resolveTimePhrase(ctx.question);
  const values = findValueMentions(ctx.question, policy);
  const pendingDraft = ctx.pending?.base ?? null;
  const previous = ctx.previous;
  const input: PlannerInput = {
    messages: buildMessages({ question: ctx.question, principal: ctx.principal, timeHint: hint, values, previous }, policy),
    schema: PLANNER_JSON_SCHEMA,
    meta: { request_id: ctx.requestId, role: ctx.principal.role, turn: ctx.turn },
    question: ctx.question,
    timeHint: hint,
    values,
    previous,
    allowedMetrics: policy.roles[ctx.principal.role].metrics,
  };

  let output: PlannerOutput | null = null;
  let result = { ...base };
  for (let attempt = 1; attempt <= 2 && output === null; attempt++) {
    result.attempts = attempt;
    try {
      const call = await ctx.llm.plan(input);
      result.llmMs += call.ms;
      if (call.gatewayLogId) result.gatewayLogId = call.gatewayLogId;
      if (call.usage) result.usage = call.usage;
      output = parsePlannerOutput(call.raw);
      if (output === null) result.invalidOutput = true;
    } catch (err) {
      result.invalidOutput = true;
      result.llmError = err instanceof LlmError ? err.kind : "error";
      if (err instanceof LlmError && err.gatewayLogId) result.gatewayLogId = err.gatewayLogId;
    }
  }
  if (output === null) {
    const options = policy.roles[ctx.principal.role].metrics.slice(0, 4).map((m, i) => ({
      id: `o${i + 1}`,
      label: policy.metrics[m].label,
      patch: { metric: m },
    }));
    result = { ...result, output: { action: "clarify", question: REPHRASE_QUESTION, options, draft: {} } };
    return result;
  }
  if (output.action === "query") {
    const draft = { ...output.intent };
    // A reply to a pending clarification refines that clarification's draft.
    if (pendingDraft) {
      if (draft.groupBy.length === 0 && pendingDraft.groupBy) draft.groupBy = pendingDraft.groupBy;
      if (draft.filters.length === 0 && pendingDraft.filters) draft.filters = pendingDraft.filters;
      if (!grainHint(ctx.question) && draft.grain === "none" && pendingDraft.grain) draft.grain = pendingDraft.grain;
    }
    const pendingRange =
      pendingDraft?.start && pendingDraft.end && ISO.test(pendingDraft.start) && ISO.test(pendingDraft.end)
        ? { start: pendingDraft.start, end: pendingDraft.end }
        : null;
    const fallback = pendingRange ?? (previous ? previous.timeRange : null);
    const sameKind = previous && policy.metrics[previous.metric].kind === policy.metrics[draft.metric].kind;
    const intent = resolveDraft(draft, hint, ctx.question, pendingRange ?? (sameKind ? fallback : null), policy);
    return { ...result, output, intent };
  }
  if (output.action === "clarify") {
    const draft: Partial<DraftIntent> = { ...output.draft };
    if (hint) {
      draft.start = hint.start;
      draft.end = hint.end;
    }
    return { ...result, output: { ...output, draft } };
  }
  return { ...result, output };
}
