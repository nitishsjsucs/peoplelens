// Workers AI through AI Gateway (production). One builder makes every options
// object, with the same shape on every call: gateway id, collectLog, custom
// metadata (ids only), the metadata-only header, and returnRawResponse so the
// gateway log id is read from this response's header. env.AI.aiGatewayLogId is
// never used: workerd keeps it (and the call options) on the shared binding, so
// concurrent requests in one isolate could swap them.
import type { PlannerInput, PlanCall, LlmClient, LlmErrorKind, TokenUsage } from "./types";
import { LlmError } from "./types";

export const METADATA_ONLY_HEADER = "cf-aig-collect-log-payload";
export const GATEWAY_LOG_HEADER = "cf-aig-log-id";

/** Workers AI internal error codes (developers.cloudflare.com/workers-ai/platform/errors/, 2026-10-10). */
export const WORKERS_AI_ERRORS = {
  /** HTTP 429: "You have used up your daily free allocation of 10,000 neurons." */
  dailyAllocation: 3036,
  /** HTTP 429: "Capacity temporarily exceeded". */
  capacity: 3040,
  /** HTTP 403: "This model requires a Workers Paid plan." */
  paidModel: 5035,
} as const;

function collect(value: unknown, codes: number[], messages: string[], depth = 0): void {
  if (depth > 4 || value === null || value === undefined) return;
  if (typeof value === "string") {
    messages.push(value);
    return;
  }
  if (typeof value === "number") return;
  if (Array.isArray(value)) {
    for (const v of value) collect(v, codes, messages, depth + 1);
    return;
  }
  if (typeof value === "object") {
    const o = value as Record<string, unknown>;
    if (typeof o["code"] === "number") codes.push(o["code"]);
    if (typeof o["code"] === "string" && /^\d+$/.test(o["code"])) codes.push(Number(o["code"]));
    for (const key of ["errors", "error", "message", "messages", "result"]) collect(o[key], codes, messages, depth + 1);
  }
}

/**
 * Classify a failed Workers AI call from its status and body. The body shape on the
 * binding path with returnRawResponse is not documented (SPEC.md 22 risk 24), so the
 * internal code is looked for anywhere in the usual envelope fields (`errors[].code`,
 * `code`, `error`) and the documented message is matched as a fallback, including a
 * leading "3036:" in a plain-text body.
 */
export function classifyWorkersAiError(status: number, text: string): LlmErrorKind {
  const codes: number[] = [];
  const messages: string[] = [];
  try {
    collect(JSON.parse(text), codes, messages);
  } catch {
    messages.push(text);
  }
  const all = messages.join("\n");
  for (const m of all.matchAll(/\b(30\d\d|50\d\d)\b/g)) codes.push(Number(m[1]));
  if (codes.includes(WORKERS_AI_ERRORS.dailyAllocation) || /daily free allocation/i.test(all)) return "quota";
  if (codes.includes(WORKERS_AI_ERRORS.paidModel) || /requires a Workers Paid plan/i.test(all)) return "paid_model";
  if (codes.includes(WORKERS_AI_ERRORS.capacity) || /capacity temporarily exceeded/i.test(all)) return "capacity";
  if (/JSON Mode couldn'?t be met/i.test(all)) return "json_mode";
  return status === 408 || status === 504 ? "timeout" : "upstream";
}

const KIND_MESSAGES: Record<LlmErrorKind, string> = {
  quota: "Workers AI daily free allocation is used up (3036)",
  capacity: "Workers AI capacity temporarily exceeded (3040)",
  paid_model: "the planner model requires Workers Paid (5035)",
  json_mode: "JSON Mode couldn't be met",
  timeout: "Workers AI timed out",
  upstream: "Workers AI returned an error",
  parse: "Workers AI returned a non-JSON body",
};

export interface WorkersAiCallOptions {
  gateway: { id: string; collectLog: true; metadata: { request_id: string; role: string; turn: number } };
  extraHeaders: { [METADATA_ONLY_HEADER]: "false" };
  returnRawResponse: true;
}

export function workersAiOptions(gatewayId: string, meta: PlannerInput["meta"]): WorkersAiCallOptions {
  return {
    gateway: { id: gatewayId, collectLog: true, metadata: { request_id: meta.request_id, role: meta.role, turn: meta.turn } },
    extraHeaders: { [METADATA_ONLY_HEADER]: "false" },
    returnRawResponse: true,
  };
}

type RawRun = (model: string, inputs: Record<string, unknown>, options: WorkersAiCallOptions) => Promise<Response>;

export class WorkersAiClient implements LlmClient {
  readonly provider = "workers_ai" as const;

  constructor(
    private readonly ai: Ai,
    readonly model: string,
    private readonly gatewayId: string,
    private readonly now: () => number = () => Date.now(),
  ) {}

  async plan(input: PlannerInput): Promise<PlanCall> {
    const started = this.now();
    const run = this.ai.run.bind(this.ai) as unknown as RawRun;
    let res: Response;
    try {
      res = await run(
        this.model,
        {
          messages: input.messages,
          response_format: { type: "json_schema", json_schema: input.schema },
          temperature: 0,
          max_tokens: 300,
        },
        workersAiOptions(this.gatewayId, input.meta),
      );
    } catch (err) {
      // returnRawResponse means non-2xx answers do not throw; a throw is a transport
      // failure, or an error the binding raised itself (its message may carry the code).
      const kind = classifyWorkersAiError(0, err instanceof Error ? err.message : String(err));
      throw new LlmError(kind, KIND_MESSAGES[kind]);
    }
    const gatewayLogId = res.headers.get(GATEWAY_LOG_HEADER);
    const text = await res.text();
    if (!res.ok) {
      const kind = classifyWorkersAiError(res.status, text);
      throw new LlmError(kind, `${KIND_MESSAGES[kind]}: HTTP ${res.status}`, gatewayLogId);
    }
    let body: unknown;
    try {
      body = JSON.parse(text);
    } catch {
      throw new LlmError("parse", "Workers AI returned a non-JSON body", gatewayLogId);
    }
    const envelope = body as { result?: { response?: unknown; usage?: TokenUsage }; response?: unknown; usage?: TokenUsage };
    const payload = envelope.result ?? envelope;
    const call: PlanCall = { raw: payload.response, ms: this.now() - started, gatewayLogId };
    if (payload.usage) call.usage = payload.usage;
    return call;
  }
}
