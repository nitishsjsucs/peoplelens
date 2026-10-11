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

export class LlmError extends Error {
  constructor(
    readonly kind: "json_mode" | "upstream" | "timeout" | "parse",
    message: string,
    readonly gatewayLogId: string | null = null,
  ) {
    super(message);
    this.name = "LlmError";
  }
}
