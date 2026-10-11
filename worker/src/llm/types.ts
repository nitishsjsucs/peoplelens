import type { QueryIntent, RoleId } from "../policy/types";
import type { TimeHint } from "../planner/timephrases";
import type { ValueMention } from "../planner/values";

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface TokenUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
}

/** Everything a provider needs. Structured fields serve the stub; real models get `messages`. */
export interface PlannerInput {
  messages: ChatMessage[];
  schema: Record<string, unknown>;
  /** AI Gateway custom metadata: ids only, never question text, emails or results. */
  meta: { request_id: string; role: RoleId; turn: number };
  question: string;
  timeHint: TimeHint | null;
  values: ValueMention[];
  previous: QueryIntent | null;
  allowedMetrics: string[];
}

export interface PlanCall {
  raw: unknown;
  usage?: TokenUsage;
  ms: number;
  gatewayLogId?: string | null;
}

export interface LlmClient {
  readonly provider: "workers_ai" | "openai_compat" | "stub";
  readonly model: string;
  plan(input: PlannerInput): Promise<PlanCall>;
}

/**
 * Why a provider call failed. Workers AI on the Free plan adds three kinds
 * (SPEC.md 25.4 B): "quota" (error 3036, the account's daily free allocation is
 * used up, or this repo's share in AccountBudgetDO is), "capacity" (3040, retried
 * once) and "paid_model" (5035, the model needs Workers Paid: a configuration error).
 */
export type LlmErrorKind = "json_mode" | "upstream" | "timeout" | "parse" | "quota" | "capacity" | "paid_model";

export class LlmError extends Error {
  constructor(
    readonly kind: LlmErrorKind,
    message: string,
    readonly gatewayLogId: string | null = null,
  ) {
    super(message);
    this.name = "LlmError";
  }
}

/**
 * This repo's daily Workers AI neuron share, bound to one UTC day (AccountBudgetDO,
 * counter "neurons"). Only the Workers AI provider meters neurons.
 */
export interface NeuronBudget {
  reserve(amount: number): Promise<{ ok: boolean; used: number; cap: number }>;
  settle(reserved: number, actual: number | null): Promise<void>;
}

/** Workers AI neurons for a token usage at a model's pinned rates (per million tokens), to 0.01. */
export function neuronsFor(usage: TokenUsage | null | undefined, rates: { input: number; output: number } | null | undefined): number | null {
  if (!usage || !rates) return null;
  const n = ((usage.prompt_tokens ?? 0) * rates.input + (usage.completion_tokens ?? 0) * rates.output) / 1_000_000;
  return Math.round(n * 100) / 100;
}
