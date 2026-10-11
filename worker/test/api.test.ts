import { beforeAll, describe, expect, it } from "vitest";
import { harness, ORIGIN, RecordingLlm } from "./helpers/app";
import { FakeWarehouse } from "./helpers/fakeWarehouse";
import { plan as planJson } from "./helpers/fakeAi";
import { ROLE_EMAILS, type TestKeys, makeKeys, mintToken, userClaims } from "./helpers/keys";
import { createApp } from "../src/index";
import { DUCK_CATALOG } from "./helpers/app";
import { localEnv } from "./helpers/env";
import { fakeAi } from "./helpers/fakeAi";
import { WorkersAiClient } from "../src/llm/workersAi";
import { secondsUntilUtcMidnight } from "../src/ask";

let keys: TestKeys;
beforeAll(async () => {
  keys = await makeKeys();
});

type Json = Record<string, any>;
const A = ROLE_EMAILS.people_analytics;
const MIB = 1024 * 1024;
const SEVEN = ["Engineering", "Sales", "Customer Success", "Marketing", "Finance", "People", "Operations"];

describe("authentication and authorization", () => {
  it("401 without the Access header, 403 for an unbound identity, 200 for a bound one", async () => {
    const app = createApp({ catalog: DUCK_CATALOG });
    const env = localEnv(keys);
    const none = await app.fetch(new Request(`${ORIGIN}/api/me`), env);
    expect(none.status).toBe(401);
    const stranger = await mintToken(keys, { claims: userClaims("stranger@example.test") });
    const forbidden = await app.fetch(new Request(`${ORIGIN}/api/me`, { headers: { "Cf-Access-Jwt-Assertion": stranger } }), env);
    expect(forbidden.status).toBe(403);
    expect(((await forbidden.json()) as Json).refusal.code).toBe("NO_ROLE_BINDING");
    const h = harness(keys);
    const me = await h.call(ROLE_EMAILS.hr_business_partner, "/api/me");
    expect(me.status).toBe(200);
    expect(me.json).toMatchObject({ role: "hr_business_partner", scope: { departments: ["Engineering", "Sales"] } });
    const health = await app.fetch(new Request(`${ORIGIN}/api/health`), env);
    expect(await health.json()).toMatchObject({ ok: true, environment: "local", warehouse: "duckdb_local", llm_provider: "stub", catalog_entries: 312 });
  });
});

describe("/api/ask", () => {
  it("answers a governed question and suppresses Legal (8) plus a complementary cell", async () => {
    const h = harness(keys);
    const s = await h.session(A);
    const r = (await h.ask(A, s, "headcount by department")) as Json;
    expect(r.type).toBe("answer");
    expect(r.meta).toMatchObject({ catalog_key: "headcount|department|none", warehouse: "fake", llm_provider: "stub" });
    expect(r.answer.columns).toEqual([
      { name: "department", kind: "dimension" },
      { name: "headcount", kind: "measure", unit: "employees" },
    ]);
    const byDept = Object.fromEntries((r.answer.rows as Json[]).map((x) => [x.department, x]));
    expect(byDept.Legal).toEqual({ department: "Legal", headcount: null, suppressed: true });
    expect(byDept.People.suppressed).toBe(true);
    expect(byDept.Engineering).toEqual({ department: "Engineering", headcount: 620, suppressed: false });
    expect(r.answer.suppressed_cells).toEqual({ primary: 1, complementary: 1, hidden_sum: 0, complement: 0 });
    expect(JSON.stringify(r)).not.toContain("cohort");
    expect(r.answer.narrative).not.toMatch(/\b8\b/);
    expect(r.answer.narrative).not.toContain(" 90");
  });

  it("blocks the two-question differencing attack (1,780 then 1,772)", async () => {
    const llm = new RecordingLlm(undefined, [
      planJson({ metric: "headcount" }),
      planJson({ metric: "headcount", filters: [{ dimension: "department", values: SEVEN }] }),
    ]);
    const h = harness(keys, { llm });
    const s = await h.session(A);
    const total = (await h.ask(A, s, "total headcount")) as Json;
    expect(total.answer.rows).toEqual([{ headcount: 1780, suppressed: false }]);
    const others = (await h.ask(A, s, "headcount excluding Legal")) as Json;
    expect(others.type).toBe("answer");
    expect(others.answer.rows).toEqual([{ headcount: null, suppressed: true }]);
    expect(others.answer.suppressed_cells.complement).toBe(1);
    expect(others.meta.probes).toBe(1);
    // the probe ran, but its numbers never left the Worker
    expect(JSON.stringify(others)).not.toContain("1780");
  });

  it("clarifies an ambiguous question, then resolves the chosen option without another LLM call", async () => {
    const h = harness(keys);
    const s = await h.session(A);
    const c = (await h.ask(A, s, "How is Engineering doing in 2024?")) as Json;
    expect(c.type).toBe("clarification");
    expect(c.clarification.options.length).toBeGreaterThan(1);
    const calls = h.llm.inputs.length;
    const option = (c.clarification.options as Json[]).find((o) => o.label === "Headcount");
    const a = (await h.choose(A, s, option!.id)) as Json;
    expect(h.llm.inputs.length).toBe(calls);
    expect(a.type).toBe("answer");
    expect(a.answer.intent).toMatchObject({
      metric: "headcount",
      time_range: { start: "2024-01-01", end: "2024-12-31" },
      filters: [{ dimension: "department", values: ["Engineering"] }],
    });
    const bad = await h.call(A, "/api/ask", { session_id: s, choose_option: "o9" });
    expect(bad.status).toBe(400);
  });

  it("follow-ups modify the previous intent", async () => {
    const h = harness(keys);
    const s = await h.session(A);
    const first = (await h.ask(A, s, "turnover by department in 2024")) as Json;
    expect(first.answer.intent).toMatchObject({ metric: "turnover_rate", group_by: ["department"], time_range: { start: "2024-01-01" } });
    const second = (await h.ask(A, s, "now by location")) as Json;
    expect(second.answer.intent).toMatchObject({ metric: "turnover_rate", group_by: ["location"], time_range: { start: "2024-01-01", end: "2024-12-31" } });
    // the planner saw the previous intent but never any result values
    const lastInput = JSON.stringify(h.llm.inputs.at(-1));
    expect(lastInput).toContain("Previous query");
    expect(lastInput).not.toContain("suppressed");
  });

  it("refuses unapproved dimensions and turns a disallowed grain into a clarification", async () => {
    const h = harness(keys);
    const s = await h.session(A);
    const g = (await h.ask(A, s, "headcount by gender")) as Json;
    expect(g.refusal.code).toBe("DIMENSION_NOT_APPROVED");
    const E = ROLE_EMAILS.executive;
    const es = await h.session(E);
    const m = (await h.ask(E, es, "monthly headcount in 2025")) as Json;
    expect(m.type).toBe("clarification");
    expect(m.clarification.question).toBe("Monthly is not available to your role. Show quarterly instead?");
    const q = (await h.choose(E, es, (m.clarification.options as Json[])[0]!.id)) as Json;
    expect(q.answer.intent).toMatchObject({ grain: "quarter", time_range: { start: "2025-01-01", end: "2025-12-31" } });
    const job = (await h.ask(E, es, "headcount by job level")) as Json;
    expect(job.refusal.code).toBe("DIMENSION_NOT_APPROVED");
    expect(job.refusal.alternatives).toEqual(["department", "location"]);
  });

  it("keeps an HRBP inside the assigned departments", async () => {
    const h = harness(keys);
    const H = ROLE_EMAILS.hr_business_partner;
    const s = await h.session(H);
    const r = (await h.ask(H, s, "headcount by department")) as Json;
    expect((r.answer.rows as Json[]).map((x) => x.department).sort()).toEqual(["Engineering", "Sales"]);
    const run = h.warehouse.calls.find((c) => c.kind === "run");
    expect(run?.q.params["scope_departments"]?.value).toEqual(["Engineering", "Sales"]);
    const legal = (await h.ask(H, s, "headcount in Legal")) as Json;
    expect(legal.refusal.code).toBe("SCOPE_VIOLATION");
  });

  it("rejects oversized results before touching the warehouse", async () => {
    const llm = new RecordingLlm(undefined, [
      planJson({ metric: "headcount", group_by: ["department", "job_level"], grain: "month", start: "2023-01-01", end: "2025-12-31" }),
    ]);
    const h = harness(keys, { llm });
    const s = await h.session(A);
    const r = (await h.ask(A, s, "headcount for everything, every month, by department and level")) as Json;
    expect(r.refusal.code).toBe("RESULT_TOO_LARGE");
    expect(h.warehouse.calls).toEqual([]);
  });

  it("enforces the per-query byte cap and the daily budget", async () => {
    const big = harness(keys, { warehouse: new FakeWarehouse(undefined, 150 * MIB) });
    const E = ROLE_EMAILS.executive;
    const s = await big.session(E);
    const r = (await big.ask(E, s, "headcount")) as Json;
    expect(r.refusal.code).toBe("COST_LIMIT_EXCEEDED");
    expect(big.warehouse.calls.every((c) => c.kind === "dryRun")).toBe(true);

    const h = harness(keys, { warehouse: new FakeWarehouse(undefined, 190 * MIB), tick: 3000 });
    const as = await h.session(A);
    let last: Json = {};
    for (let i = 0; i < 27; i++) last = (await h.ask(A, as, "headcount")) as Json;
    expect(last.refusal.code).toBe("DAILY_BUDGET_EXCEEDED");
    const runs = h.warehouse.calls.filter((c) => c.kind === "run");
    expect(runs).toHaveLength(26);
    for (const c of runs) expect(c.o?.maxBytesBilled).toBe(200 * MIB);
  });

  it("rate-limits at 30 questions per minute per user", async () => {
    const h = harness(keys, { tick: 100 });
    const R = ROLE_EMAILS.talent_acquisition;
    const s = await h.session(R);
    const statuses: number[] = [];
    for (let i = 0; i < 31; i++) statuses.push((await h.call(R, "/api/ask", { session_id: s, message: "headcount" })).status);
    expect(statuses.slice(0, 30).every((x) => x === 200)).toBe(true);
    expect(statuses[30]).toBe(429);
  });

  it("validates the request body", async () => {
    const h = harness(keys);
    const s = await h.session(A);
    expect((await h.call(A, "/api/ask", { message: "headcount" })).status).toBe(400);
    expect((await h.call(A, "/api/ask", { session_id: s, message: "x", choose_option: "o1" })).status).toBe(400);
    expect((await h.call(A, "/api/ask", { session_id: s, message: "x".repeat(501) })).status).toBe(400);
    expect((await h.call(A, "/api/ask", { session_id: "nope", message: "headcount" })).status).toBe(404);
  });
});

describe("Workers AI daily allowance (Free plan)", () => {
  const MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";
  const errorResponse = (status: number, code: number, message: string) =>
    new Response(JSON.stringify({ success: false, errors: [{ code, message }] }), { status, headers: { "cf-aig-log-id": "log-q" } });

  it("3036 gives AI_DAILY_LIMIT (503, Retry-After to 00:00 UTC) after exactly one Workers AI call", async () => {
    const { ai, calls } = fakeAi([errorResponse(429, 3036, "You have used up your daily free allocation of 10,000 neurons.")]);
    const h = harness(keys, { llm: new RecordingLlm(new WorkersAiClient(ai, MODEL, "peoplelens")) });
    const s = await h.session(A);
    const r = await h.call(A, "/api/ask", { session_id: s, message: "headcount by department" });
    expect(calls).toHaveLength(1);
    expect(r.status).toBe(503);
    expect(r.json).toMatchObject({ type: "refusal", refusal: { code: "AI_DAILY_LIMIT" } });
    expect(r.headers.get("Retry-After")).toBe(String(secondsUntilUtcMidnight(h.clock.t)));
    expect(Number(r.headers.get("Retry-After"))).toBeGreaterThan(0);
    expect((r.json as Json).meta).toMatchObject({ decision: "AI_DAILY_LIMIT", gateway_log_id: "log-q", planner_attempts: 1, llm_usage: null });
    expect(h.warehouse.calls).toEqual([]);
  });

  it("5035 (a model that needs Workers Paid) is a configuration error, not a clarification", async () => {
    const { ai, calls } = fakeAi([errorResponse(403, 5035, "This model requires a Workers Paid plan.")]);
    const h = harness(keys, { llm: new RecordingLlm(new WorkersAiClient(ai, MODEL, "peoplelens")) });
    const s = await h.session(A);
    const r = await h.call(A, "/api/ask", { session_id: s, message: "headcount by department" });
    expect(calls).toHaveLength(1);
    expect(r.status).toBe(503);
    expect((r.json as Json).refusal.code).toBe("CONFIG_INVALID");
  });

  it("meta.llm_usage carries token counts only", async () => {
    const { ai } = fakeAi([planJsonFor("headcount", ["department"])]);
    const h = harness(keys, { llm: new RecordingLlm(new WorkersAiClient(ai, MODEL, "peoplelens")) });
    const s = await h.session(A);
    const r = (await h.ask(A, s, "headcount by department")) as Json;
    expect(r.type).toBe("answer");
    expect(Object.keys(r.meta.llm_usage).sort()).toEqual(["completion_tokens", "neurons", "prompt_tokens"]);
    expect(r.meta.llm_usage.prompt_tokens).toBe(900);
    expect(r.meta.llm_usage.completion_tokens).toBe(60);
    const stub = harness(keys);
    const s2 = await stub.session(A);
    expect(((await stub.ask(A, s2, "headcount by department")) as Json).meta.llm_usage).toBeNull();
  });

  it("Retry-After counts down to the next 00:00 UTC", () => {
    expect(secondsUntilUtcMidnight(Date.UTC(2026, 9, 10, 23, 59, 30))).toBe(30);
    expect(secondsUntilUtcMidnight(Date.UTC(2026, 9, 10, 0, 0, 0))).toBe(86400);
    expect(secondsUntilUtcMidnight(Date.UTC(2026, 9, 10, 12))).toBe(43200);
  });
});

function planJsonFor(metric: string, groupBy: string[]): Record<string, unknown> {
  return planJson({ metric, group_by: groupBy });
}
