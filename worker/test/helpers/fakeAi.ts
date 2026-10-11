// A fake Workers AI binding: records every run() call and answers with a raw
// Response carrying a cf-aig-log-id header, like the binding with returnRawResponse.
export interface AiCall {
  model: string;
  inputs: Record<string, unknown>;
  options: Record<string, unknown>;
}

export function fakeAi(responses: (unknown | Response)[], logIds: string[] = []) {
  const calls: AiCall[] = [];
  let n = 0;
  const ai = {
    aiGatewayLogId: "WRONG-shared-binding-value",
    async run(model: string, inputs: Record<string, unknown>, options: Record<string, unknown>) {
      calls.push({ model, inputs, options });
      const i = n++;
      const r = responses[Math.min(i, responses.length - 1)];
      if (r instanceof Response) return r;
      return new Response(JSON.stringify({ response: r, usage: { prompt_tokens: 900, completion_tokens: 60 } }), {
        headers: { "content-type": "application/json", "cf-aig-log-id": logIds[i] ?? `log-${i + 1}` },
      });
    },
  };
  return { ai: ai as unknown as Ai, calls };
}

export function plan(p: Record<string, unknown>): Record<string, unknown> {
  return {
    action: "query",
    metric: "headcount",
    group_by: [],
    grain: "none",
    start: "",
    end: "",
    filters: [],
    clarification_question: "",
    clarification_options: [],
    refusal_reason: "",
    ...p,
  };
}
