import { describe, expect, it } from "vitest";
import { OpenAiCompatClient } from "../src/llm/openaiCompat";
import { PLANNER_JSON_SCHEMA } from "../src/planner/schema";
import { plan as planJson } from "./helpers/fakeAi";

describe("OpenAI-compatible client (local llama-server)", () => {
  it("requests json_schema output with thinking disabled", async () => {
    const seen: { url: string; body: Record<string, unknown> }[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      seen.push({ url: String(input), body: JSON.parse(String(init?.body)) as Record<string, unknown> });
      return Response.json({ choices: [{ message: { content: JSON.stringify(planJson({ metric: "fte" })) } }], usage: { prompt_tokens: 10 } });
    };
    const client = new OpenAiCompatClient("http://127.0.0.1:8180/v1/", "qwen3-1.7b", fetcher);
    const res = await client.plan({
      messages: [{ role: "user", content: "fte" }],
      schema: PLANNER_JSON_SCHEMA,
      meta: { request_id: "r", role: "executive", turn: 1 },
      question: "fte",
      timeHint: null,
      values: [],
      previous: null,
      allowedMetrics: [],
    });
    expect(seen[0]?.url).toBe("http://127.0.0.1:8180/v1/chat/completions");
    expect(seen[0]?.body).toMatchObject({
      model: "qwen3-1.7b",
      temperature: 0,
      max_tokens: 300,
      chat_template_kwargs: { enable_thinking: false },
      response_format: { type: "json_schema", json_schema: { name: "planner_output", schema: PLANNER_JSON_SCHEMA } },
    });
    expect(JSON.parse(res.raw as string)).toMatchObject({ metric: "fte" });
    expect(res.gatewayLogId).toBeNull();
  });

  it("surfaces server errors and missing content", async () => {
    const bad = new OpenAiCompatClient("http://127.0.0.1:8180/v1", "m", async () => new Response("x", { status: 500 }));
    await expect(bad.plan({ messages: [], schema: {}, meta: { request_id: "r", role: "executive", turn: 1 }, question: "", timeHint: null, values: [], previous: null, allowedMetrics: [] })).rejects.toMatchObject({ kind: "upstream" });
    const empty = new OpenAiCompatClient("http://127.0.0.1:8180/v1", "m", async () => Response.json({ choices: [] }));
    await expect(empty.plan({ messages: [], schema: {}, meta: { request_id: "r", role: "executive", turn: 1 }, question: "", timeHint: null, values: [], previous: null, allowedMetrics: [] })).rejects.toMatchObject({ kind: "parse" });
  });
});
