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
import { ground } from "./grounding";
import { findBreakdowns, findMetricCues, findValueMentions, screenRequest } from "./values";

const FOLLOW_UP = /^\s*(and|what about|how about|now|same|instead|ok|okay|also|then|just|only)\b/i;

export interface PlanContext {
  question: string;
  principal: Principal;
  previous: QueryIntent | null;
  pending: PendingClarification | null;
  llm: LlmClient;
  requestId: string;
  turn: number;
  policy?: Policy;
  /** Wait before retrying a Workers AI capacity error (3040); injectable for tests. */
  sleep?: (ms: number) => Promise<void>;
}

export interface PlanResult {
  output: PlannerOutput;
  intent: QueryIntent | null;
  llmMs: number;
  gatewayLogId: string | null;
  /** Token counts summed over every attempt that reported usage (counts only, no text). */
  usage: TokenUsage | null;
  attempts: number;
  invalidOutput: boolean;
  screened: boolean;
  screenMessage: string | null;
  llmError: string | null;
  /**
   * Set when the planner stopped without asking the user to rephrase: "quota" (Workers
   * AI 3036, or this repo's daily share refused) becomes AI_DAILY_LIMIT; "paid_model"
   * (5035) is a configuration error. Neither is retried.
   */
  llmFailure: "quota" | "paid_model" | null;
}

/** Milliseconds a capacity error (3040) waits before its one retry. */
export const CAPACITY_RETRY_MS = 1000;

function addUsage(total: TokenUsage | null, add: TokenUsage | undefined): TokenUsage | null {
  if (!add) return total;
  const prompt = (total?.prompt_tokens ?? 0) + (add.prompt_tokens ?? 0);
  const completion = (total?.completion_tokens ?? 0) + (add.completion_tokens ?? 0);
  return { prompt_tokens: prompt, completion_tokens: completion, total_tokens: prompt + completion };
}

const ISO = /^\d{4}-\d{2}-\d{2}$/;
const TIME_WORDS =
  /\b(20\d\d|fy\d\d|q[1-4]|h[12]|january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|months?|quarters?|years?|weeks?|since|ago|past|previous|prior)\b/i;

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Does the question name a governed metric (cue words or a policy alias)? */
export function namesMetric(question: string, policy: Policy = POLICY): boolean {
  if (findMetricCues(question).length > 0) return true;
  const t = question.toLowerCase();
  return Object.values(policy.metrics).some((m) =>
    m.aliases.some((a) => new RegExp(`(?<![a-z0-9])${escapeRe(a.toLowerCase())}(?![a-z0-9])`).test(t)),
  );
}

/** "Headcount, please." after an answer or a clarification: a reply that only names a metric. */
export function isShortMetricReply(question: string, policy: Policy = POLICY): boolean {
  const words = question.toLowerCase().replace(/[^a-z0-9\s-]/g, " ").split(/\s+/).filter(Boolean);
  if (words.length === 0 || words.length > 4) return false;
  return (
    namesMetric(question, policy) &&
    findValueMentions(question, policy).length === 0 &&
    findBreakdowns(question).length === 0 &&
    resolveTimePhrase(question) === null
  );
}

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
  // Defaults are enforced server-side: model dates are used only when the question
  // has time words the deterministic resolver could not read.
  const modelDates = draft.start && draft.end && ISO.test(draft.start) && ISO.test(draft.end) && TIME_WORDS.test(question);
  if (hint) range = rangeFor(kind, hint);
  else if (fallback) range = fallback;
  else if (modelDates && draft.start && draft.end) range = { start: draft.start, end: draft.end };
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
    screenMessage: null,
    llmError: null,
    llmFailure: null,
  };

  const screen = screenRequest(ctx.question);
  if (screen) return { ...base, output: { action: "refuse", reason: screen.code }, screened: true, screenMessage: screen.message };

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
  const sleep = ctx.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  for (let attempt = 1; attempt <= 2 && output === null; attempt++) {
    result.attempts = attempt;
    try {
      const call = await ctx.llm.plan(input);
      result.llmMs += call.ms;
      if (call.gatewayLogId) result.gatewayLogId = call.gatewayLogId;
      result.usage = addUsage(result.usage, call.usage);
      output = parsePlannerOutput(call.raw);
      if (output === null) result.invalidOutput = true;
    } catch (err) {
      result.llmError = err instanceof LlmError ? err.kind : "error";
      if (err instanceof LlmError && err.gatewayLogId) result.gatewayLogId = err.gatewayLogId;
      // The daily allocation and a paid-only model are not transient: never retry them,
      // and never answer "Could you rephrase?" for them.
      if (err instanceof LlmError && (err.kind === "quota" || err.kind === "paid_model")) {
        return { ...result, llmFailure: err.kind };
      }
      result.invalidOutput = true;
      if (err instanceof LlmError && err.kind === "capacity" && attempt < 2) await sleep(CAPACITY_RETRY_MS);
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
  // A refusal of an in-domain but ambiguous question (it names approved values but
  // no metric, and the deterministic screen found nothing to refuse) becomes a
  // clarification. Refusals for PII, writes, scope and metrics are always kept.
  if (
    output.action === "refuse" &&
    (output.reason === "OUT_OF_DOMAIN" || output.reason === "DIMENSION_NOT_APPROVED") &&
    !namesMetric(ctx.question, policy) &&
    (values.length > 0 || findBreakdowns(ctx.question).length > 0)
  ) {
    output = {
      action: "query",
      intent: {
        metric: previous?.metric ?? policy.roles[ctx.principal.role].metrics[0] ?? "headcount",
        groupBy: [],
        grain: "none",
        start: "",
        end: "",
        filters: [],
      },
    };
  }
  const followUp =
    previous !== null &&
    (findMetricCues(ctx.question).length === 0 || FOLLOW_UP.test(ctx.question) || isShortMetricReply(ctx.question, policy));
  const groundCtx = { question: ctx.question, previous, followUp, pending: pendingDraft, policy };
  if (output.action === "query" && !followUp && !pendingDraft && !namesMetric(ctx.question, policy)) {
    // The metric is a slot too: a question that names no metric is ambiguous, so ask
    // instead of guessing (the model's guess is offered first).
    const draft = ground(output.intent, groundCtx);
    const allowed = policy.roles[ctx.principal.role].metrics;
    const order = [output.intent.metric, ...allowed].filter((m, i, a) => allowed.includes(m) && a.indexOf(m) === i);
    const options = order.slice(0, 4).map((m, i) => ({ id: `o${i + 1}`, label: policy.metrics[m].label, patch: { metric: m } }));
    const scope = draft.filters.flatMap((f) => f.values);
    const base: Partial<DraftIntent> = { groupBy: draft.groupBy, grain: draft.grain, filters: draft.filters };
    if (hint) {
      base.start = hint.start;
      base.end = hint.end;
    }
    const question = `Which measure would you like${scope.length > 0 && scope.length <= 3 ? ` for ${scope.join(", ")}` : ""}?`;
    return { ...result, output: { action: "clarify", question, options, draft: base } };
  }
  if (output.action === "query") {
    const draft = ground(output.intent, groundCtx);
    const pendingRange =
      pendingDraft?.start && pendingDraft.end && ISO.test(pendingDraft.start) && ISO.test(pendingDraft.end)
        ? { start: pendingDraft.start, end: pendingDraft.end }
        : null;
    // A follow-up keeps the conversation's period, except that a point-in-time month
    // (a stock metric's default) does not become a one-month period for a rate.
    const pointMonth =
      previous !== null &&
      policy.metrics[previous.metric].kind === "stock" &&
      policy.metrics[draft.metric].kind === "flow" &&
      previous.timeRange.start.slice(0, 7) === previous.timeRange.end.slice(0, 7);
    const fallback = pendingRange ?? (followUp && previous && !pointMonth ? previous.timeRange : null);
    const intent = resolveDraft(draft, hint, ctx.question, fallback, policy);
    return { ...result, output: { action: "query", intent: draft }, intent };
  }
  if (output.action === "clarify") {
    const base = ground(
      {
        metric: output.draft.metric ?? "headcount",
        groupBy: output.draft.groupBy ?? [],
        grain: output.draft.grain ?? "none",
        start: "",
        end: "",
        filters: output.draft.filters ?? [],
      },
      groundCtx,
    );
    const draft: Partial<DraftIntent> = { groupBy: base.groupBy, grain: base.grain, filters: base.filters };
    if (output.draft.metric) draft.metric = output.draft.metric;
    if (hint) {
      draft.start = hint.start;
      draft.end = hint.end;
    }
    return { ...result, output: { ...output, draft } };
  }
  return { ...result, output };
}
