// Workers AI through AI Gateway (production). One builder makes every options
// object, with the same shape on every call: gateway id, collectLog, custom
// metadata (ids only), the metadata-only header, and returnRawResponse so the
// gateway log id is read from this response's header. env.AI.aiGatewayLogId is
// never used: workerd keeps it (and the call options) on the shared binding, so
// concurrent requests in one isolate could swap them.
import type { PlannerInput, PlanCall, LlmClient, TokenUsage } from "./types";
import { LlmError } from "./types";

export const METADATA_ONLY_HEADER = "cf-aig-collect-log-payload";
export const GATEWAY_LOG_HEADER = "cf-aig-log-id";

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
    const res = await run(
      this.model,
      {
        messages: input.messages,
        response_format: { type: "json_schema", json_schema: input.schema },
        temperature: 0,
        max_tokens: 300,
      },
      workersAiOptions(this.gatewayId, input.meta),
    );
    const gatewayLogId = res.headers.get(GATEWAY_LOG_HEADER);
    const text = await res.text();
    if (!res.ok) {
      if (/JSON Mode couldn'?t be met/i.test(text)) throw new LlmError("json_mode", "JSON Mode couldn't be met", gatewayLogId);
      throw new LlmError("upstream", `Workers AI returned ${res.status}`, gatewayLogId);
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
