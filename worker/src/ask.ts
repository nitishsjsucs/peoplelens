// The /api/ask pipeline (SPEC.md section 3.3): rate limit, plan (one LLM call),
// validate, resolve the catalog entry, cost guard, execute main query and
// complement probes, suppress, narrate, record the turn. Every safeguard after
// planning is server-side and independent of the LLM.
import { narrate } from "./answer/narrative";
import { type BoundQuery, type Dialect, bindQuery } from "./catalog/bind";
import { type CatalogFile, catalogKey, lookupEntry } from "./catalog/catalog";
import { applyComplementRule, complementProbes } from "./guard/complement";
import { checkCost, rowUpperBound } from "./guard/cost";
import { type SuppressionCounts, suppressCells, toCells } from "./guard/suppression";
import type { LlmClient, TokenUsage } from "./llm/types";
import { logEvent } from "./log";
import { plan, resolveDraft } from "./planner/planner";
import type { DraftIntent } from "./planner/schema";
import { POLICY } from "./policy/policy";
import { ApiError, type Policy, type Principal, type QueryIntent, type RefusalCode } from "./policy/types";
import { DIMENSION_VALUES, type DimensionValues, type Validated, validateIntent } from "./policy/validate";
import type { BudgetDecision, PendingClarification, RateDecision, SessionState, TurnRecord } from "./session/userDO";
import type { RunResult, Warehouse } from "./warehouse/types";

export interface UserPort {
  reserveQuestion(now: number, limit?: number): Promise<RateDecision>;
  reserveBytes(day: string, bytes: number, cap: number): Promise<BudgetDecision>;
  settleBytes(day: string, reserved: number, billed: number | null): Promise<void>;
  getSession(id: string): Promise<SessionState | null>;
  saveTurn(id: string, turn: TurnRecord): Promise<boolean>;
  createSession(now: number): Promise<string>;
}

export interface AskDeps {
  catalog: CatalogFile;
  dialect: Dialect;
  projectId?: string;
  warehouse: Warehouse;
  llm: LlmClient;
  user: UserPort;
  principal: Principal;
  requestId: string;
  now: () => number;
  policy?: Policy;
  values?: DimensionValues;
}

export interface AskBody {
  session_id?: unknown;
  message?: unknown;
  choose_option?: unknown;
}

export interface AnswerRow {
  [column: string]: string | number | boolean | null;
}

export interface AskMeta {
  latency_ms: number;
  llm_ms: number;
  warehouse_ms: number;
  estimated_bytes: number;
  billed_bytes: number | null;
  catalog_key: string | null;
  warehouse: Warehouse["kind"];
  llm_provider: LlmClient["provider"];
  model: string;
  gateway_log_id: string | null;
  decision: string | null;
  probes: number;
  planner_attempts: number;
  /** Token counts summed over the planner's attempts, and Workers AI neurons when the
   * model has a pinned rate (null otherwise). Counts only, never text. Null when no
   * attempt reported usage (no LLM call, or a provider without usage). */
  llm_usage: LlmUsage | null;
  estimate_note?: string;
}

export interface LlmUsage {
  prompt_tokens: number;
  completion_tokens: number;
  neurons: number | null;
}

export function llmUsageMeta(usage: TokenUsage | null): LlmUsage | null {
  if (!usage) return null;
  return { prompt_tokens: usage.prompt_tokens ?? 0, completion_tokens: usage.completion_tokens ?? 0, neurons: null };
}

/** Seconds from `now` (epoch ms) to the next 00:00 UTC, when the Workers AI allowance resets. */
export function secondsUntilUtcMidnight(now: number): number {
  const d = new Date(now);
  const next = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1);
  return Math.max(1, Math.ceil((next - now) / 1000));
}

export interface AskResponse {
  request_id: string;
  type: "answer" | "clarification" | "refusal";
  answer?: {
    metric: string;
    intent: {
      metric: string;
      group_by: string[];
      grain: string;
      time_range: { start: string; end: string };
      filters: { dimension: string; values: string[] }[];
    };
    definition: string;
    columns: { name: string; kind: "dimension" | "time" | "measure"; unit?: string }[];
    rows: AnswerRow[];
    suppressed_cells: SuppressionCounts;
    narrative: string;
  };
  clarification?: { question: string; options: { id: string; label: string }[] };
  refusal?: { code: RefusalCode | "BAD_REQUEST" | "NOT_FOUND"; message: string; alternatives: string[] };
  meta: AskMeta;
}

const REFUSAL_MESSAGES: Partial<Record<RefusalCode, string>> = {
  WRITE_REQUEST: "PeopleLens is read-only: it cannot change, add or delete any data.",
  PII_REQUEST: "PeopleLens answers with aggregates only; it never returns names, contact details, identifiers or person-level rows.",
  DIMENSION_NOT_APPROVED: "That breakdown is not an approved dimension for your role.",
  METRIC_NOT_ALLOWED: "That metric is not available to your role.",
  OUT_OF_DOMAIN: "I can answer questions about the six governed workforce metrics only.",
  SCOPE_VIOLATION: "That is outside the data your role can see.",
  PLANNER_INVALID_OUTPUT: "I could not turn that into a governed query.",
  AI_DAILY_LIMIT: "PeopleLens has used its Workers AI allowance for today. It resets at 00:00 UTC; please try again then.",
};

async function sha16(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function intentJson(i: QueryIntent): NonNullable<AskResponse["answer"]>["intent"] {
  return {
    metric: i.metric,
    group_by: i.groupBy,
    grain: i.grain,
    time_range: { start: i.timeRange.start, end: i.timeRange.end },
    filters: i.filters.map((f) => ({ dimension: f.dimension, values: f.values })),
  };
}

function draftOf(i: QueryIntent): Partial<DraftIntent> {
  return {
    metric: i.metric,
    groupBy: i.groupBy,
    grain: i.grain,
    start: i.timeRange.start,
    end: i.timeRange.end,
    filters: i.filters,
  };
}

export interface AskResult {
  status: number;
  body: AskResponse;
  headers?: Record<string, string>;
}

export async function ask(deps: AskDeps, body: AskBody): Promise<AskResult> {
  const policy = deps.policy ?? POLICY;
  const values = deps.values ?? DIMENSION_VALUES;
  const limits = policy.limits;
  const t0 = deps.now();
  const meta: AskMeta = {
    latency_ms: 0,
    llm_ms: 0,
    warehouse_ms: 0,
    estimated_bytes: 0,
    billed_bytes: null,
    catalog_key: null,
    warehouse: deps.warehouse.kind,
    llm_provider: deps.llm.provider,
    model: deps.llm.model,
    gateway_log_id: null,
    decision: null,
    probes: 0,
    planner_attempts: 0,
    llm_usage: null,
  };
  let sessionId: string | null = null;
  let turnRecord: TurnRecord | null = null;

  const finish = async (status: number, res: Omit<AskResponse, "request_id" | "meta">, headers?: Record<string, string>): Promise<AskResult> => {
    meta.latency_ms = deps.now() - t0;
    if (sessionId && turnRecord) await deps.user.saveTurn(sessionId, turnRecord);
    const out: AskResponse = { request_id: deps.requestId, ...res, meta };
    logEvent({
      event: "ask",
      request_id: deps.requestId,
      role: deps.principal.role,
      action: res.type,
      catalog_key: meta.catalog_key,
      decision: meta.decision,
      status,
      latency_ms: meta.latency_ms,
      llm_ms: meta.llm_ms,
      warehouse_ms: meta.warehouse_ms,
      estimated_bytes: meta.estimated_bytes,
      billed_bytes: meta.billed_bytes,
      suppressed: res.answer?.suppressed_cells ?? null,
      provider: meta.llm_provider,
      model: meta.model,
      gateway_log_id: meta.gateway_log_id,
      probes: meta.probes,
      prompt_tokens: meta.llm_usage?.prompt_tokens ?? null,
      completion_tokens: meta.llm_usage?.completion_tokens ?? null,
      neurons: meta.llm_usage?.neurons ?? null,
    });
    return headers ? { status, body: out, headers } : { status, body: out };
  };

  const refusal = (
    code: RefusalCode | "BAD_REQUEST" | "NOT_FOUND",
    message?: string,
    alternatives: string[] = [],
    status = 200,
    headers?: Record<string, string>,
  ) => {
    meta.decision = code;
    if (sessionId) {
      turnRecord = { ts: deps.now(), type: "refusal", intent: null, pending: null, code, catalogKey: meta.catalog_key };
    }
    return finish(
      status,
      {
        type: "refusal",
        refusal: { code, message: message ?? REFUSAL_MESSAGES[code as RefusalCode] ?? "Request refused.", alternatives },
      },
      headers,
    );
  };

  const clarification = (pending: PendingClarification) => {
    meta.decision = meta.decision ?? "CLARIFY";
    turnRecord = { ts: deps.now(), type: "clarification", intent: null, pending, code: null, catalogKey: null };
    return finish(200, {
      type: "clarification",
      clarification: { question: pending.question, options: pending.options.map((o) => ({ id: o.id, label: o.label })) },
    });
  };

  // 0. request shape
  if (typeof body.session_id !== "string" || body.session_id.length === 0 || body.session_id.length > 64) {
    return refusal("BAD_REQUEST", "session_id is required", [], 400);
  }
  const hasMessage = typeof body.message === "string";
  const hasChoice = typeof body.choose_option === "string";
  if (hasMessage === hasChoice) return refusal("BAD_REQUEST", "send exactly one of message or choose_option", [], 400);
  if (hasMessage && ((body.message as string).trim().length === 0 || (body.message as string).length > 500)) {
    return refusal("BAD_REQUEST", "message must be 1 to 500 characters", [], 400);
  }

  // 1. rate limit (per user, UserDO)
  const rate = await deps.user.reserveQuestion(t0, limits.rate_limit_per_minute);
  if (!rate.ok) return refusal("RATE_LIMITED", "Too many questions; try again in a minute.", [], 429);

  // 2. session
  const session = await deps.user.getSession(body.session_id);
  if (!session) return refusal("NOT_FOUND", "unknown session", [], 404);
  sessionId = session.id;
  const turn = session.turns + 1;

  // 3. plan (or resolve a chosen option without an LLM call)
  let intent: QueryIntent;
  if (hasChoice) {
    const option = session.pending?.options.find((o) => o.id === body.choose_option);
    if (!session.pending || !option) return refusal("BAD_REQUEST", "no pending clarification has that option", [], 400);
    const draft = { ...(session.pending.base ?? {}), ...option.patch };
    if (!draft.metric) return refusal("BAD_REQUEST", "that option does not name a metric", [], 400);
    const fallback = draft.start && draft.end ? { start: draft.start, end: draft.end } : null;
    intent = resolveDraft({ ...draft, metric: draft.metric }, null, "", fallback, policy);
  } else {
    const planned = await plan({
      question: body.message as string,
      principal: deps.principal,
      previous: session.lastIntent,
      pending: session.pending,
      llm: deps.llm,
      requestId: deps.requestId,
      turn,
      policy,
    });
    meta.llm_ms = planned.llmMs;
    meta.gateway_log_id = planned.gatewayLogId;
    meta.planner_attempts = planned.attempts;
    meta.llm_usage = llmUsageMeta(planned.usage);
    if (planned.llmFailure === "quota") {
      // Workers AI 3036 (the account's daily free allocation is used up): a clear,
      // retryable refusal instead of a misleading "Could you rephrase?".
      return refusal("AI_DAILY_LIMIT", undefined, [], 503, { "Retry-After": String(secondsUntilUtcMidnight(deps.now())) });
    }
    if (planned.llmFailure === "paid_model") {
      throw new ApiError(503, "CONFIG_INVALID", "the planner model requires Workers Paid; set PLANNER_MODEL to a Free plan model (see make doctor)");
    }
    if (planned.invalidOutput && planned.output.action === "clarify") meta.decision = "PLANNER_INVALID_OUTPUT";
    const out = planned.output;
    if (out.action === "refuse") return refusal(out.reason, planned.screenMessage ?? undefined);
    if (out.action === "clarify") {
      return clarification({
        question: out.question,
        options: out.options.map((o) => ({ id: o.id, label: o.label, patch: o.patch as Partial<DraftIntent> })),
        base: out.draft,
      });
    }
    if (!planned.intent) return refusal("PLANNER_INVALID_OUTPUT");
    intent = planned.intent;
  }

  // 4. validate against the role policy
  const validation = validateIntent(intent, deps.principal, policy, values);
  if (!validation.ok) {
    const v = validation.violation;
    if (v.clarify) {
      meta.decision = v.code;
      return clarification({
        question: v.clarify.question,
        options: v.clarify.options.map((o) => ({ id: o.id, label: o.label, patch: { grain: o.patch.grain ?? "none" } })),
        base: draftOf(intent),
      });
    }
    return refusal(v.code, v.message, v.alternatives);
  }
  const validated: Validated = validation.value;

  // 5. resolve the compiled catalog entry
  const key = catalogKey(validated);
  meta.catalog_key = key;
  const entry = lookupEntry(deps.catalog, key);
  if (!entry) throw new ApiError(500, "INTERNAL", "no catalog entry for a validated intent");

  // 6. row bound before any execution
  const rows = rowUpperBound(entry, validated, values);
  if (rows > limits.max_rows) {
    return refusal(
      "RESULT_TOO_LARGE",
      `That would return up to ${rows} rows (limit ${limits.max_rows}). Try a coarser grain, a shorter period or fewer breakdowns.`,
      ["quarter", "year", "one breakdown dimension"],
    );
  }

  // 7. complement probes, binding, dry runs and the cost guard
  const probes = complementProbes(validated);
  meta.probes = probes.length;
  const queries: BoundQuery[] = [validated, ...probes.map((p) => p.validated)].map((v) =>
    bindQuery(entry, v, deps.dialect, deps.projectId, policy, values),
  );
  let estimates: number[];
  try {
    estimates = (await Promise.all(queries.map((q) => deps.warehouse.dryRun(q)))).map((e) => e.estimatedBytes);
  } catch (err) {
    if (err instanceof ApiError && err.code === "COST_LIMIT_EXCEEDED") return refusal(err.code, err.message);
    throw err;
  }
  meta.estimated_bytes = estimates.reduce((s, e) => s + e, 0);
  if (deps.warehouse.kind !== "bigquery") meta.estimate_note = "local estimate of BigQuery on-demand bytes, not a BigQuery dry run";
  const cost = await checkCost(estimates, deps.principal, limits, deps.user, deps.now());
  if (!cost.ok) return refusal(cost.code, cost.message);

  // 8. execute the main query and the probes in parallel; probes never leave the Worker
  const labels = { app: "peoplelens", role: deps.principal.role, catalog_key_hash: await sha16(key) };
  let results: RunResult[];
  try {
    results = await Promise.all(
      queries.map((q) => deps.warehouse.run(q, { maxBytesBilled: cost.perQueryCap, timeoutMs: limits.query_timeout_ms, labels })),
    );
  } catch (err) {
    if (err instanceof ApiError && err.code === "COST_LIMIT_EXCEEDED") return refusal(err.code, err.message);
    throw err;
  }
  meta.warehouse_ms = Math.max(...results.map((r) => r.elapsedMs));
  const billed = results.every((r) => r.billedBytes !== null) ? results.reduce((s, r) => s + (r.billedBytes ?? 0), 0) : null;
  meta.billed_bytes = billed;
  await deps.user.settleBytes(cost.day, cost.reservedBytes, billed);

  // 9. suppression: primary, complement rule, complementary, hidden sum
  const k = limits.min_cohort;
  const colmap = entry.columns as { dimensions: Record<string, string>; time: string | null; values: string[]; cohort: string };
  const mainCells = toCells(results[0] ?? { columns: [], rows: [] }, colmap, entry.group_by);
  const probeCells = results.slice(1).map((r) => toCells(r, colmap, entry.group_by));
  const { suppressKeys } = applyComplementRule(mainCells, probeCells, { k });
  const suppressed = suppressCells(mainCells, { dims: entry.group_by, k, forced: suppressKeys });

  // 10. answer
  const metric = policy.metrics[validated.metric];
  const columns: NonNullable<AskResponse["answer"]>["columns"] = [
    ...validated.groupBy.map((d) => ({ name: d, kind: "dimension" as const })),
    ...(validated.grain !== "none" ? [{ name: "period", kind: "time" as const }] : []),
    ...metric.mf_metrics.map((m) => ({ name: m, kind: "measure" as const, unit: metric.unit })),
  ];
  const answerRows: AnswerRow[] = suppressed.rows.map((r) => {
    const row: AnswerRow = {};
    for (const d of validated.groupBy) row[d] = r.dims[d] ?? null;
    if (validated.grain !== "none") row["period"] = r.period;
    for (const m of metric.mf_metrics) row[m] = r.values[m] ?? null;
    row["suppressed"] = r.suppressed;
    return row;
  });
  meta.decision = "ANSWER";
  turnRecord = { ts: deps.now(), type: "answer", intent: validated.intent, pending: null, code: null, catalogKey: key };
  return finish(200, {
    type: "answer",
    answer: {
      metric: metric.id,
      intent: intentJson(validated.intent),
      definition: metric.definition,
      columns,
      rows: answerRows,
      suppressed_cells: suppressed.counts,
      narrative: narrate(metric, validated.intent, suppressed.rows, suppressed.counts, policy),
    },
  });
}
