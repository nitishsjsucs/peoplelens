import { describe, expect, it } from "vitest";
import { METADATA_ONLY_HEADER, WorkersAiClient, classifyWorkersAiError, workersAiOptions } from "../src/llm/workersAi";
import { LlmError, type PlannerInput } from "../src/llm/types";
import { CAPACITY_RETRY_MS, plan } from "../src/planner/planner";
import { PLANNER_JSON_SCHEMA, parsePlannerOutput } from "../src/planner/schema";
import { REFUSAL_CODES } from "../src/policy/types";
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

  it("a model can never choose the server-only AI_DAILY_LIMIT refusal", () => {
    expect(REFUSAL_CODES).toContain("AI_DAILY_LIMIT");
    const schemaEnum = (PLANNER_JSON_SCHEMA["properties"] as Record<string, { enum?: string[] }>)["refusal_reason"]?.enum ?? [];
    expect(schemaEnum).not.toContain("AI_DAILY_LIMIT");
    expect(parsePlannerOutput(planJson({ action: "refuse", refusal_reason: "AI_DAILY_LIMIT" }))).toEqual({ action: "refuse", reason: "OUT_OF_DOMAIN" });
  });
});

// Workers AI on the Free plan (SPEC.md 25.4 B): error bodies as documented on the
// errors page, in the REST envelope and in the plainer shapes the binding may use.
const err = (status: number, body: unknown, logId = "log-err") =>
  new Response(typeof body === "string" ? body : JSON.stringify(body), { status, headers: { "cf-aig-log-id": logId } });
const ENVELOPE_3036 = { success: false, errors: [{ code: 3036, message: "You have used up your daily free allocation of 10,000 neurons." }] };
const ENVELOPE_3040 = { success: false, errors: [{ code: 3040, message: "Capacity temporarily exceeded" }] };
const ENVELOPE_5035 = { success: false, errors: [{ code: 5035, message: "This model requires a Workers Paid plan." }] };

describe("Workers AI errors on the Free plan", () => {
  it("classifies 3036, 3040 and 5035 by code or by the documented message", () => {
    expect(classifyWorkersAiError(429, JSON.stringify(ENVELOPE_3036))).toBe("quota");
    expect(classifyWorkersAiError(429, JSON.stringify({ errors: [{ message: "You have used up your daily free allocation of 10,000 neurons." }] }))).toBe("quota");
    expect(classifyWorkersAiError(429, "3036: You have used up your daily free allocation")).toBe("quota");
    expect(classifyWorkersAiError(429, JSON.stringify({ error: { code: "3036" } }))).toBe("quota");
    expect(classifyWorkersAiError(429, JSON.stringify(ENVELOPE_3040))).toBe("capacity");
    expect(classifyWorkersAiError(403, JSON.stringify(ENVELOPE_5035))).toBe("paid_model");
    expect(classifyWorkersAiError(400, JSON.stringify({ errors: [{ message: "JSON Mode couldn't be met" }] }))).toBe("json_mode");
    expect(classifyWorkersAiError(500, "internal error")).toBe("upstream");
    expect(classifyWorkersAiError(429, JSON.stringify({ errors: [{ code: 3007, message: "Rate limited" }] }))).toBe("upstream");
  });

  it("the client raises typed errors and keeps the gateway log id", async () => {
    await expect(new WorkersAiClient(fakeAi([err(429, ENVELOPE_3036)]).ai, MODEL, "g").plan(input())).rejects.toMatchObject({ kind: "quota", gatewayLogId: "log-err" });
    await expect(new WorkersAiClient(fakeAi([err(429, ENVELOPE_3040)]).ai, MODEL, "g").plan(input())).rejects.toMatchObject({ kind: "capacity" });
    await expect(new WorkersAiClient(fakeAi([err(403, ENVELOPE_5035)]).ai, MODEL, "g").plan(input())).rejects.toMatchObject({ kind: "paid_model" });
    const throwing = { async run() { throw new Error("3036: daily free allocation used up"); } } as unknown as Ai;
    await expect(new WorkersAiClient(throwing, MODEL, "g").plan(input())).rejects.toMatchObject({ kind: "quota" });
  });

  const ctx = (ai: Ai, sleeps: number[] = []) => ({
    question: "headcount by department",
    principal: ANALYST,
    previous: null,
    pending: null,
    llm: new WorkersAiClient(ai, MODEL, "g"),
    requestId: "r",
    turn: 1,
    sleep: async (ms: number) => void sleeps.push(ms),
  });

  it("3036 stops after exactly one Workers AI call, with no rephrase clarification", async () => {
    const { ai, calls } = fakeAi([err(429, ENVELOPE_3036), planJson({ group_by: ["department"] })]);
    const r = await plan(ctx(ai));
    expect(calls).toHaveLength(1);
    expect(r.llmFailure).toBe("quota");
    expect(r.attempts).toBe(1);
    expect(r.output.action).not.toBe("clarify");
    expect(r.intent).toBeNull();
    expect(r.gatewayLogId).toBe("log-err");
  });

  it("5035 is a configuration error, never retried", async () => {
    const { ai, calls } = fakeAi([err(403, ENVELOPE_5035), planJson({})]);
    const r = await plan(ctx(ai));
    expect(calls).toHaveLength(1);
    expect(r.llmFailure).toBe("paid_model");
  });

  it("3040 is retried once after 1 s, then the answer proceeds", async () => {
    const sleeps: number[] = [];
    const { ai, calls } = fakeAi([err(429, ENVELOPE_3040), planJson({ group_by: ["department"] })]);
    const r = await plan(ctx(ai, sleeps));
    expect(calls).toHaveLength(2);
    expect(sleeps).toEqual([CAPACITY_RETRY_MS]);
    expect(CAPACITY_RETRY_MS).toBe(1000);
    expect(r.llmFailure).toBeNull();
    expect(r.output.action).toBe("query");
    expect(r.usage).toEqual({ prompt_tokens: 900, completion_tokens: 60, total_tokens: 960 });
  });

  it("two capacity errors in a row end in the rephrase clarification, without a second wait", async () => {
    const sleeps: number[] = [];
    const { ai, calls } = fakeAi([err(429, ENVELOPE_3040), err(429, ENVELOPE_3040)]);
    const r = await plan(ctx(ai, sleeps));
    expect(calls).toHaveLength(2);
    expect(sleeps).toEqual([CAPACITY_RETRY_MS]);
    expect(r.llmFailure).toBeNull();
    expect(r.output.action).toBe("clarify");
    expect(r.llmError).toBe("capacity");
  });

  it("usage is summed over both attempts when the first output is invalid", async () => {
    const { ai, calls } = fakeAi([{ action: "nonsense" }, planJson({ group_by: ["department"] })]);
    const r = await plan(ctx(ai));
    expect(calls).toHaveLength(2);
    expect(r.usage).toEqual({ prompt_tokens: 1800, completion_tokens: 120, total_tokens: 1920 });
  });
});

