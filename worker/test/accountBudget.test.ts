// AccountBudgetDO: this repo's share of the account's daily Free plan allowances
// (SPEC.md 25.4 B), run against the real SQLite Durable Object in workerd.
import { env } from "cloudflare:workers";
import { beforeAll, describe, expect, it } from "vitest";
import type { Env } from "../src/env";
import { budgetPort } from "../src/index";
import { type NeuronBudget, neuronsFor } from "../src/llm/types";
import { WorkersAiClient } from "../src/llm/workersAi";
import { POLICY } from "../src/policy/policy";
import { ACCOUNT_BUDGET_NAME, KEEP_DAYS, utcDay } from "../src/session/accountBudgetDO";
import { harness } from "./helpers/app";
import { fakeAi, plan as planJson } from "./helpers/fakeAi";
import { ROLE_EMAILS, type TestKeys, makeKeys } from "./helpers/keys";

const testEnv = env as unknown as Env;
const MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";
const FREE = POLICY.limits.free_plan;
const RATES = FREE.model_neurons_per_m_tokens[MODEL]!;
const A = ROLE_EMAILS.people_analytics;

function budget(name = ACCOUNT_BUDGET_NAME) {
  return testEnv.ACCOUNT_BUDGET.get(testEnv.ACCOUNT_BUDGET.idFromName(name));
}

let keys: TestKeys;
beforeAll(async () => {
  keys = await makeKeys();
});

describe("AccountBudgetDO", () => {
  it("grants reservations up to the cap and refuses the one that would cross it", async () => {
    const b = budget("cap-test");
    expect(await b.reserve("2026-01-01", "neurons", 5800, 6000)).toEqual({ ok: true, used: 5800, cap: 6000 });
    expect(await b.reserve("2026-01-01", "neurons", 200, 6000)).toEqual({ ok: true, used: 6000, cap: 6000 });
    expect(await b.reserve("2026-01-01", "neurons", 1, 6000)).toEqual({ ok: false, used: 6000, cap: 6000 });
    expect((await b.usage("2026-01-01")).neurons).toBe(6000);
  });

  it("settle replaces a reservation with the actual amount (rounded up); null keeps it", async () => {
    const b = budget("settle-test");
    await b.reserve("2026-01-02", "neurons", 200, 6000);
    await b.settle("2026-01-02", "neurons", 200, 36.29);
    expect((await b.usage("2026-01-02")).neurons).toBe(37);
    await b.reserve("2026-01-02", "neurons", 200, 6000);
    await b.settle("2026-01-02", "neurons", 200, null);
    expect((await b.usage("2026-01-02")).neurons).toBe(237);
    await b.reserve("2026-01-02", "neurons", 200, 6000);
    await b.settle("2026-01-02", "neurons", 200, 0);
    expect((await b.usage("2026-01-02")).neurons).toBe(237);
  });

  it("keeps counters and UTC days separate", async () => {
    const b = budget("separate-test");
    await b.reserve("2026-01-03", "neurons", 100, 6000);
    await b.reserve("2026-01-03", "audit_queue_messages", 1, 400);
    await b.reserve("2026-01-03", "audit_direct_puts", 2, 2000);
    await b.reserve("2026-01-04", "neurons", 7, 6000);
    expect(await b.usage("2026-01-03")).toEqual({ neurons: 100, audit_queue_messages: 1, audit_direct_puts: 2 });
    expect(await b.usage("2026-01-04")).toEqual({ neurons: 7, audit_queue_messages: 0, audit_direct_puts: 0 });
  });

  it("concurrent reservations never exceed the cap", async () => {
    const b = budget("concurrency-test");
    const decisions = await Promise.all(Array.from({ length: 50 }, () => b.reserve("2026-01-05", "neurons", FREE.planner_reserve_neurons, FREE.worker_neurons_per_day)));
    expect(decisions.filter((d) => d.ok)).toHaveLength(FREE.worker_neurons_per_day / FREE.planner_reserve_neurons);
    expect((await b.usage("2026-01-05")).neurons).toBe(FREE.worker_neurons_per_day);
    const audit = await Promise.all(Array.from({ length: 900 }, () => b.reserve("2026-01-05", "audit_queue_messages", 1, FREE.audit_queue_messages_per_day)));
    expect(audit.filter((d) => d.ok)).toHaveLength(FREE.audit_queue_messages_per_day);
  });

  it("refuses invalid input as a value (fails closed) and prunes days older than a week", async () => {
    const b = budget("invalid-test");
    expect((await b.reserve("yesterday", "neurons", 1, 6000)).ok).toBe(false);
    expect((await b.reserve("2026-01-06", "neurons", -5, 6000)).ok).toBe(false);
    expect((await b.reserve("2026-01-06", "bogus" as "neurons", 1, 6000)).ok).toBe(false);
    expect((await b.reserve("2026-01-06", "neurons", Number.NaN, 6000)).ok).toBe(false);
    await b.reserve("2026-01-01", "neurons", 5, 6000);
    await b.reserve(`2026-01-${String(1 + KEEP_DAYS + 1).padStart(2, "0")}`, "neurons", 1, 6000);
    expect((await b.usage("2026-01-01")).neurons).toBe(0);
  });

  it("utcDay is the UTC calendar day", () => {
    expect(utcDay(Date.UTC(2026, 9, 10, 23, 59, 59))).toBe("2026-10-10");
    expect(utcDay(Date.UTC(2026, 9, 11, 0, 0, 0))).toBe("2026-10-11");
  });
});

class RecordingBudget implements NeuronBudget {
  reserves: number[] = [];
  settles: [number, number | null][] = [];
  constructor(private grant = true) {}
  async reserve(amount: number) {
    this.reserves.push(amount);
    return { ok: this.grant, used: this.grant ? amount : 6000, cap: 6000 };
  }
  async settle(reserved: number, actual: number | null) {
    this.settles.push([reserved, actual]);
  }
}

describe("planner reservations around every Workers AI attempt", () => {
  const input = {
    messages: [{ role: "user" as const, content: "q" }],
    schema: {},
    meta: { request_id: "r", role: "people_analytics" as const, turn: 1 },
    question: "q",
    timeHint: null,
    values: [],
    previous: null,
    allowedMetrics: [],
  };
  const client = (ai: Ai, b: NeuronBudget) =>
    new WorkersAiClient(ai, MODEL, "g", () => 0, { port: b, reserveNeurons: FREE.planner_reserve_neurons, rates: RATES });

  it("reserves 200 first and settles with the neurons the returned usage costs", async () => {
    const b = new RecordingBudget();
    const { ai, calls } = fakeAi([planJson({})]);
    await client(ai, b).plan(input);
    expect(calls).toHaveLength(1);
    expect(b.reserves).toEqual([200]);
    const expected = neuronsFor({ prompt_tokens: 900, completion_tokens: 60 }, RATES);
    expect(expected).toBe(36.29);
    expect(b.settles).toEqual([[200, expected]]);
  });

  it("a refused reservation raises quota with zero Workers AI calls", async () => {
    const b = new RecordingBudget(false);
    const { ai, calls } = fakeAi([planJson({})]);
    await expect(client(ai, b).plan(input)).rejects.toMatchObject({ kind: "quota" });
    expect(calls).toHaveLength(0);
    expect(b.settles).toEqual([]);
  });

  it("3036 settles the reservation to 0; an unknown failure keeps it", async () => {
    const b = new RecordingBudget();
    const quota = new Response(JSON.stringify({ errors: [{ code: 3036, message: "daily free allocation" }] }), { status: 429 });
    await expect(client(fakeAi([quota]).ai, b).plan(input)).rejects.toMatchObject({ kind: "quota" });
    const jsonMode = new Response(JSON.stringify({ errors: [{ message: "JSON Mode couldn't be met" }] }), { status: 400 });
    await expect(client(fakeAi([jsonMode]).ai, b).plan(input)).rejects.toMatchObject({ kind: "json_mode" });
    expect(b.settles).toEqual([[200, 0], [200, null]]);
  });

  it("the reservation is at least the worst case of one planner call at the pinned rates", () => {
    // SPEC.md 25.5: 4,654 input tokens (with template, schema and a 15% margin) and max_tokens 300.
    expect(neuronsFor({ prompt_tokens: 4654, completion_tokens: 300 }, RATES)).toBeLessThanOrEqual(FREE.planner_reserve_neurons);
  });
});

describe("the Worker with this repo's share", () => {
  const withBudget = (aiResponses: unknown[]) => {
    const fake = fakeAi(aiResponses);
    const make = (e: Env, now: number) =>
      new WorkersAiClient(fake.ai, MODEL, "peoplelens", () => now, {
        port: budgetPort(e, "neurons", FREE.worker_neurons_per_day, now),
        reserveNeurons: FREE.planner_reserve_neurons,
        rates: RATES,
      });
    return { fake, make };
  };

  it("a used-up share gives AI_DAILY_LIMIT (503) with zero Workers AI calls", async () => {
    const { fake, make } = withBudget([planJson({ group_by: ["department"] })]);
    const h = harness(keys, { makeLlm: make });
    h.clock.t = Date.UTC(2026, 10, 1, 12);
    await budget().reserve("2026-11-01", "neurons", FREE.worker_neurons_per_day - 100, FREE.worker_neurons_per_day);
    const s = await h.session(A);
    const r = await h.call(A, "/api/ask", { session_id: s, message: "headcount by department" });
    expect(r.status).toBe(503);
    expect((r.json as Record<string, any>).refusal.code).toBe("AI_DAILY_LIMIT");
    expect(r.headers.get("Retry-After")).not.toBeNull();
    expect(fake.calls).toHaveLength(0);
  });

  it("an answered question settles to its usage and /api/me reports the day's share", async () => {
    const { fake, make } = withBudget([planJson({ group_by: ["department"] })]);
    const h = harness(keys, { makeLlm: make });
    h.clock.t = Date.UTC(2026, 10, 2, 12);
    const s = await h.session(A);
    const r = (await h.ask(A, s, "headcount by department")) as Record<string, any>;
    expect(r.type).toBe("answer");
    expect(fake.calls).toHaveLength(1);
    expect(r.meta.llm_usage).toEqual({ prompt_tokens: 900, completion_tokens: 60, neurons: 36.29 });
    const me = await h.call(A, "/api/me");
    expect(me.json["ai_budget"]).toEqual({ day: "2026-11-02", used: 37, cap: FREE.worker_neurons_per_day });
  });
});
