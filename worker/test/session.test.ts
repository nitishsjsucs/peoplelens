import { env } from "cloudflare:workers";
import { describe, expect, it } from "vitest";
import type { Env } from "../src/env";
import type { QueryIntent } from "../src/policy/types";

const testEnv = env as unknown as Env;
const MIB = 1024 * 1024;

function stub(name: string) {
  return testEnv.USER_DO.get(testEnv.USER_DO.idFromName(name));
}

const INTENT: QueryIntent = {
  metric: "headcount",
  groupBy: ["department"],
  grain: "none",
  timeRange: { start: "2025-12-01", end: "2025-12-31" },
  filters: [],
};

describe("UserDO rate limit", () => {
  it("allows 30 questions per minute per user, then refuses until the window passes", async () => {
    const s = stub("rate-user");
    const t0 = 1_760_000_000_000;
    for (let i = 0; i < 30; i++) expect((await s.reserveQuestion(t0 + i)).ok).toBe(true);
    const refused = await s.reserveQuestion(t0 + 100);
    expect(refused.ok).toBe(false);
    expect(refused.retryAfterMs).toBeGreaterThan(0);
    expect((await s.reserveQuestion(t0 + 60_001)).ok).toBe(true);
  });

  it("keeps users separate", async () => {
    const a = stub("rate-a");
    const b = stub("rate-b");
    const t = 1_760_000_000_000;
    for (let i = 0; i < 30; i++) await a.reserveQuestion(t);
    expect((await a.reserveQuestion(t)).ok).toBe(false);
    expect((await b.reserveQuestion(t)).ok).toBe(true);
  });
});

describe("UserDO daily byte budget", () => {
  it("reserves, refuses past the cap, and settles to billed bytes", async () => {
    const s = stub("budget-user");
    const cap = 50 * MIB;
    expect(await s.reserveBytes("2026-10-10", 30 * MIB, cap)).toEqual({ ok: true, used: 30 * MIB, cap });
    expect((await s.reserveBytes("2026-10-10", 30 * MIB, cap)).ok).toBe(false);
    await s.settleBytes("2026-10-10", 30 * MIB, 10 * MIB);
    expect(await s.budgetUsed("2026-10-10")).toBe(10 * MIB);
    expect((await s.reserveBytes("2026-10-10", 30 * MIB, cap)).ok).toBe(true);
    await s.settleBytes("2026-10-10", 30 * MIB, null);
    expect(await s.budgetUsed("2026-10-10")).toBe(40 * MIB);
    expect((await s.reserveBytes("2026-10-11", 50 * MIB, cap)).ok).toBe(true);
  });

  it("charges complement probes together with the main query", async () => {
    const s = stub("probe-user");
    const cap = 35 * MIB;
    expect((await s.reserveBytes("2026-10-10", 4 * 10 * MIB, cap)).ok).toBe(false);
    expect((await s.reserveBytes("2026-10-10", 3 * 10 * MIB, cap)).ok).toBe(true);
  });
});

describe("UserDO sessions", () => {
  it("stores the last answered intent and the pending clarification, not text", async () => {
    const s = stub("session-user");
    const id = await s.createSession(1);
    expect(await s.getSession(id)).toMatchObject({ id, lastIntent: null, pending: null, turns: 0 });
    await s.saveTurn(id, { ts: 2, type: "answer", intent: INTENT, pending: null, code: null, catalogKey: "headcount|department|none" });
    const pending = { question: "Which metric?", options: [{ id: "o1", label: "Headcount", patch: { metric: "headcount" as const } }], base: null };
    await s.saveTurn(id, { ts: 3, type: "clarification", intent: null, pending, code: null, catalogKey: null });
    const state = await s.getSession(id);
    expect(state?.lastIntent).toEqual(INTENT);
    expect(state?.pending).toEqual(pending);
    expect(state?.turns).toBe(2);
    expect(await s.getSession("missing")).toBeNull();
    expect(await s.saveTurn("missing", { ts: 4, type: "refusal", intent: null, pending: null, code: "X", catalogKey: null })).toBe(false);
  });
});
