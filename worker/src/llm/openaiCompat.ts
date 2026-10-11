// OpenAI-compatible chat completions (local llama-server with Qwen3-1.7B).
// json_schema constrained decoding; thinking disabled through chat_template_kwargs.
import type { LlmClient, PlanCall, PlannerInput, TokenUsage } from "./types";
import { LlmError } from "./types";

export class OpenAiCompatClient implements LlmClient {
  readonly provider = "openai_compat" as const;

  constructor(
    private readonly baseUrl: string,
    readonly model: string,
    private readonly fetcher: typeof fetch = (input, init) => fetch(input, init),
    private readonly timeoutMs = 20_000,
  ) {}

  async plan(input: PlannerInput): Promise<PlanCall> {
    const started = Date.now();
    const url = `${this.baseUrl.replace(/\/+$/, "")}/chat/completions`;
    let res: Response;
    try {
      res = await this.fetcher(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          model: this.model,
          messages: input.messages,
          response_format: { type: "json_schema", json_schema: { name: "planner_output", schema: input.schema } },
          chat_template_kwargs: { enable_thinking: false },
          temperature: 0,
          max_tokens: 300,
        }),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (err) {
      throw new LlmError("timeout", err instanceof Error ? err.message : "request failed");
    }
    if (!res.ok) throw new LlmError("upstream", `LLM server returned ${res.status}`);
    const json = (await res.json().catch(() => ({}))) as {
      choices?: { message?: { content?: string } }[];
      usage?: TokenUsage;
    };
    const content = json.choices?.[0]?.message?.content;
    if (typeof content !== "string") throw new LlmError("parse", "no message content");
    const call: PlanCall = { raw: content, ms: Date.now() - started, gatewayLogId: null };
    if (json.usage) call.usage = json.usage;
    return call;
  }
}
