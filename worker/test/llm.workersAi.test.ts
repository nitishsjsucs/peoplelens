import { describe, expect, it } from "vitest";
import { METADATA_ONLY_HEADER, WorkersAiClient, workersAiOptions } from "../src/llm/workersAi";
import { LlmError, type PlannerInput } from "../src/llm/types";
import { plan } from "../src/planner/planner";
import { PLANNER_JSON_SCHEMA } from "../src/planner/schema";
import { fakeAi, plan as planJson } from "./helpers/fakeAi";
import { ANALYST } from "./helpers/principals";

const MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";
const QUESTION = "what was turnover in Legal last year";

function input(turn = 1): PlannerInput {
  return {
    messages: [{ role: "user", content: QUESTION }],
    schema: PLANNER_JSON_SCHEMA,
    meta: { request_id: "req-1", role: "people_analytics", turn },
    question: QUESTION,
    timeHint: null,
    values: [],
    previous: null,
    allowedMetrics: ["headcount"],
  };
}

describe("Workers AI client", () => {
  it("every call goes through the gateway with metadata-only logging and a raw response", async () => {
    const { ai, calls } = fakeAi([planJson({}), planJson({})]);
    const client = new WorkersAiClient(ai, MODEL, "peoplelens");
    await client.plan(input(1));
    await client.plan(input(2));
    expect(calls).toHaveLength(2);
    for (const [i, call] of calls.entries()) {
      expect(call.model).toBe(MODEL);
      expect(call.options).toEqual({
        gateway: { id: "peoplelens", collectLog: true, metadata: { request_id: "req-1", role: "people_analytics", turn: i + 1 } },
        extraHeaders: { [METADATA_ONLY_HEADER]: "false" },
        returnRawResponse: true,
      });
      expect(JSON.stringify(call.options)).not.toContain(QUESTION);
      expect(call.inputs).toMatchObject({ temperature: 0, max_tokens: 300, response_format: { type: "json_schema", json_schema: PLANNER_JSON_SCHEMA } });
    }
    // one builder, identical shape on every call
    expect(Object.keys(calls[0]!.options)).toEqual(Object.keys(workersAiOptions("x", input().meta)));
  });

  it("reads the gateway log id from this response's header, never from the shared binding", async () => {
    const { ai } = fakeAi([planJson({})], ["log-abc"]);
    const res = await new WorkersAiClient(ai, MODEL, "peoplelens").plan(input());
    expect(res.gatewayLogId).toBe("log-abc");
    expect(res.usage).toEqual({ prompt_tokens: 900, completion_tokens: 60 });
  });

  it("accepts a JSON-mode object, a JSON string, and the REST-style result envelope", async () => {
    const obj = await new WorkersAiClient(fakeAi([planJson({ metric: "fte" })]).ai, MODEL, "g").plan(input());
    expect(obj.raw).toMatchObject({ metric: "fte" });
    const str = await new WorkersAiClient(fakeAi([JSON.stringify(planJson({ metric: "fte" }))]).ai, MODEL, "g").plan(input());
    expect(typeof str.raw).toBe("string");
    const env = new Response(JSON.stringify({ result: { response: planJson({ metric: "headcount" }) }, success: true }));
    const wrapped = await new WorkersAiClient(fakeAi([env]).ai, MODEL, "g").plan(input());
    expect(wrapped.raw).toMatchObject({ metric: "headcount" });
  });

  it("maps the JSON Mode failure, retries once in the planner, then asks to rephrase", async () => {
    const failure = () =>
      new Response(JSON.stringify({ errors: [{ message: "JSON Mode couldn't be met" }] }), {
        status: 400,
        headers: { "cf-aig-log-id": "log-err" },
      });
    await expect(new WorkersAiClient(fakeAi([failure()]).ai, MODEL, "g").plan(input())).rejects.toMatchObject({ kind: "json_mode" });
    const { ai, calls } = fakeAi([failure(), failure()]);
    const r = await plan({
      question: "headcount",
      principal: ANALYST,
      previous: null,
      pending: null,
      llm: new WorkersAiClient(ai, MODEL, "g"),
      requestId: "r",
      turn: 1,
    });
    expect(calls).toHaveLength(2);
    expect(r.output.action).toBe("clarify");
    expect(r.llmError).toBe("json_mode");
    expect(r.gatewayLogId).toBe("log-err");
    expect(new LlmError("upstream", "x")).toBeInstanceOf(Error);
  });
});
