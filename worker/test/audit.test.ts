// Metadata-only audit trail on the Workers Free plan (SPEC.md 25.4 A and section 13):
// queue slot or direct put, content-addressed consumer batches, retries, whitelist.
import { env as baseEnv } from "cloudflare:workers";
import { beforeAll, describe, expect, it, vi } from "vitest";
import {
  AUDIT_FIELDS,
  BATCH_KEY,
  DIRECT_KEY,
  RETRY_DELAY_S,
  batchKey,
  buildAuditEvent,
  cleanAuditRecord,
  directKey,
  emitAudit,
  sidecarStore,
  stageBatch,
  subjectHmac,
  toNdjson,
  type AuditEvent,
  type StagingStore,
} from "../src/audit/audit";
import type { Env } from "../src/env";
import { createApp, handleAuditBatch } from "../src/index";
import { POLICY } from "../src/policy/policy";
import { ACCOUNT_BUDGET_NAME, utcDay } from "../src/session/budget";
import { DUCK_CATALOG, RecordingLlm, ORIGIN } from "./helpers/app";
import { localEnv } from "./helpers/env";
import { FakeWarehouse } from "./helpers/fakeWarehouse";
import { ROLE_EMAILS, type TestKeys, makeKeys, mintToken, userClaims } from "./helpers/keys";

let keys: TestKeys;
beforeAll(async () => {
  keys = await makeKeys();
});

const MARKER = "OKAPI-MARKER-5521";
const HMAC = "k".repeat(32);
const SLOTS = POLICY.limits.free_plan.audit_queue_messages_per_day;

function fakeBatch(bodies: unknown[]) {
  const state: { acked: boolean; retried: boolean; delay: number | null } = { acked: false, retried: false, delay: null };
  const batch = {
    queue: "peoplelens-audit",
    messages: bodies.map((body, i) => ({ id: String(i), timestamp: new Date(), body, attempts: 1, ack() {}, retry() {} })),
    ackAll() {
      state.acked = true;
    },
    retryAll(opts?: { delaySeconds?: number }) {
      state.retried = true;
      state.delay = opts?.delaySeconds ?? null;
    },
  } as unknown as MessageBatch<unknown>;
  return { batch, state };
}

class MemoryBucket {
  objects = new Map<string, string>();
  failures = 0;
  async put(key: string, body: string) {
    if (this.failures > 0) {
      this.failures -= 1;
      throw new Error("R2 unavailable");
    }
    this.objects.set(key, body);
    return {};
  }
  asBinding(): R2Bucket {
    return this as unknown as R2Bucket;
  }
}

async function askOnce(env: Env, now: number, message = `${MARKER} headcount by location`): Promise<void> {
  const app = createApp({ catalog: DUCK_CATALOG, makeWarehouse: () => new FakeWarehouse(), makeLlm: () => new RecordingLlm(), now: () => now });
  const token = await mintToken(keys, { claims: userClaims(ROLE_EMAILS.executive) });
  const headers = { "Cf-Access-Jwt-Assertion": token, "content-type": "application/json" };
  const s = await app.fetch(new Request(`${ORIGIN}/api/sessions`, { method: "POST", headers, body: "{}" }), env);
  const sessionId = ((await s.json()) as { session_id: string }).session_id;
  await app.fetch(new Request(`${ORIGIN}/api/ask`, { method: "POST", headers, body: JSON.stringify({ session_id: sessionId, message }) }), env);
}

const event = (id: string, extra: Partial<AuditEvent> = {}): AuditEvent =>
  ({ ...Object.fromEntries(AUDIT_FIELDS.map((f) => [f, null])), ts: "2026-10-10T12:00:00.000Z", request_id: id, role: "executive", action: "answer", ...extra }) as AuditEvent;

describe("audit producer", () => {
  it("sends one metadata-only event per question through the queue when a slot is granted", async () => {
    const sent: unknown[] = [];
    const bucket = new MemoryBucket();
    const env = localEnv(keys, { AUDIT_HMAC_KEY: HMAC }) as Env;
    env.AUDIT_QUEUE = { send: async (m: unknown) => void sent.push(m) } as unknown as Queue<unknown>;
    env.AUDIT_BUCKET = bucket.asBinding();
    await askOnce(env, Date.UTC(2026, 11, 1, 12));
    expect(sent).toHaveLength(1);
    expect(bucket.objects.size).toBe(0);
    const e = sent[0] as Record<string, unknown>;
    expect(Object.keys(e).sort()).toEqual([...AUDIT_FIELDS].sort());
    expect(e).toMatchObject({ role: "executive", action: "answer", metric: "headcount", group_by: "location", catalog_key: "headcount|location|none", rows: 5 });
    const text = JSON.stringify(e);
    expect(text).not.toContain(MARKER);
    expect(text).not.toContain("exec@example.test");
    expect(e["subject_hmac"]).toBe(await subjectHmac(HMAC, "email:exec@example.test"));
    expect(String(e["subject_hmac"])).toMatch(/^[0-9a-f]{64}$/);
    const usage = await (baseEnv as unknown as Env).ACCOUNT_BUDGET.get((baseEnv as unknown as Env).ACCOUNT_BUDGET.idFromName(ACCOUNT_BUDGET_NAME)).usage("2026-12-01");
    expect(usage.audit_queue_messages).toBe(1);
  });

  it("sends nothing without the HMAC key", async () => {
    const sent: unknown[] = [];
    const env = localEnv(keys) as Env;
    env.AUDIT_QUEUE = { send: async (m: unknown) => void sent.push(m) } as unknown as Queue<unknown>;
    env.AUDIT_BUCKET = new MemoryBucket().asBinding();
    await askOnce(env, Date.UTC(2026, 11, 2, 12));
    expect(sent).toEqual([]);
  });

  it("when the day's queue share is used up the event goes straight to staging under direct-<request_id>", async () => {
    const sent: unknown[] = [];
    const bucket = new MemoryBucket();
    const env = localEnv(keys, { AUDIT_HMAC_KEY: HMAC }) as Env;
    env.AUDIT_QUEUE = { send: async (m: unknown) => void sent.push(m) } as unknown as Queue<unknown>;
    env.AUDIT_BUCKET = bucket.asBinding();
    const now = Date.UTC(2026, 11, 3, 12);
    const budget = env.ACCOUNT_BUDGET.get(env.ACCOUNT_BUDGET.idFromName(ACCOUNT_BUDGET_NAME));
    expect((await budget.reserve(utcDay(now), "audit_queue_messages", SLOTS, SLOTS)).ok).toBe(true);
    await askOnce(env, now);
    expect(sent).toEqual([]);
    const [key, body] = [...bucket.objects.entries()][0] ?? ["", ""];
    expect(key).toMatch(DIRECT_KEY);
    expect(key).toMatch(/^staging\/v1\/dt=2026-12-03\/direct-[0-9a-f-]{36}\.ndjson$/);
    const line = JSON.parse(body.trim()) as Record<string, unknown>;
    expect(Object.keys(line).sort()).toEqual([...AUDIT_FIELDS].sort());
    expect(key).toContain(String(line["request_id"]));
    expect(body).not.toContain(MARKER);
  });

  it("a failed send gives a direct put and releases the slot; a refused slot sends nothing", async () => {
    const store = { puts: [] as [string, string][], kind: "r2" as const, async put(k: string, b: string) { this.puts.push([k, b]); } };
    let released = 0;
    const failing = { send: async () => { throw new Error("queue down"); } } as unknown as Queue<unknown>;
    expect(await emitAudit(event("r-1"), { queue: failing, store, reserveSlot: async () => true, releaseSlot: async () => void released++ })).toBe("direct");
    expect(released).toBe(1);
    const sent: unknown[] = [];
    const queue = { send: async (m: unknown) => void sent.push(m) } as unknown as Queue<unknown>;
    expect(await emitAudit(event("r-2"), { queue, store, reserveSlot: async () => false, releaseSlot: async () => undefined })).toBe("direct");
    expect(sent).toEqual([]);
    expect(store.puts.map(([k]) => k)).toEqual(["staging/v1/dt=2026-10-10/direct-r-1.ndjson", "staging/v1/dt=2026-10-10/direct-r-2.ndjson"]);
    // A repeated put of the same request lands on the same key (idempotent).
    await emitAudit(event("r-2"), { queue, store, reserveSlot: async () => false, releaseSlot: async () => undefined });
    expect(store.puts[2]).toEqual(store.puts[1]);
    expect(await emitAudit(event("r-3"), { store: null, reserveSlot: async () => false, releaseSlot: async () => undefined })).toBe("dropped");
  });

  it("once the day's direct puts are used up as well, the event is dropped and logged, never put", async () => {
    const store = { puts: [] as [string, string][], kind: "r2" as const, async put(k: string, b: string) { this.puts.push([k, b]); } };
    const none = { reserveSlot: async () => false, releaseSlot: async () => undefined };
    expect(await emitAudit(event("d-1"), { store, ...none, reserveDirect: async () => true })).toBe("direct");
    expect(await emitAudit(event("d-2"), { store, ...none, reserveDirect: async () => false })).toBe("capped");
    expect(await emitAudit(event("d-3"), { store, ...none, reserveDirect: async () => { throw new Error("DO down"); } })).toBe("capped");
    expect(store.puts.map(([k]) => k)).toEqual(["staging/v1/dt=2026-10-10/direct-d-1.ndjson"]);
  });

  it("through /api/ask: no queue slot and no direct put left means no R2 write and an audit_dropped log", async () => {
    const bucket = new MemoryBucket();
    const env = localEnv(keys, { AUDIT_HMAC_KEY: HMAC }) as Env;
    env.AUDIT_QUEUE = { send: async () => undefined } as unknown as Queue<unknown>;
    env.AUDIT_BUCKET = bucket.asBinding();
    const now = Date.UTC(2026, 11, 4, 12);
    const budget = env.ACCOUNT_BUDGET.get(env.ACCOUNT_BUDGET.idFromName(ACCOUNT_BUDGET_NAME));
    const direct = POLICY.limits.free_plan.audit_direct_puts_per_day;
    expect((await budget.reserve(utcDay(now), "audit_queue_messages", SLOTS, SLOTS)).ok).toBe(true);
    expect((await budget.reserve(utcDay(now), "audit_direct_puts", direct, direct)).ok).toBe(true);
    const logs: string[] = [];
    const spy = vi.spyOn(console, "log").mockImplementation((line: unknown) => void logs.push(String(line)));
    try {
      await askOnce(env, now);
    } finally {
      spy.mockRestore();
    }
    expect(bucket.objects.size).toBe(0);
    expect(logs.some((l) => l.includes('"event":"audit_dropped"'))).toBe(true);
    expect((await budget.usage(utcDay(now))).audit_direct_puts).toBe(direct);
  });

  it("strips anything outside the whitelist on every path", async () => {
    const r = cleanAuditRecord({ ts: "t", request_id: "r", question: "secret", role: "executive", rows: 3, rogue: { a: 1 } });
    expect(r).not.toBeNull();
    expect(Object.keys(r ?? {})).toEqual([...AUDIT_FIELDS]);
    expect(JSON.stringify(r)).not.toContain("secret");
    expect(cleanAuditRecord({ role: "x" })).toBeNull();
    const store = { puts: [] as [string, string][], kind: "r2" as const, async put(k: string, b: string) { this.puts.push([k, b]); } };
    await emitAudit({ ...event("r-9"), question: "secret" } as unknown as AuditEvent, { store, reserveSlot: async () => false, releaseSlot: async () => undefined });
    expect(store.puts[0]?.[1]).not.toContain("secret");
    const staged = await stageBatch([{ ...event("r-8"), question: "secret" }, { nonsense: true }], store);
    expect(staged?.rows).toBe(1);
    expect(store.puts[1]?.[1]).not.toContain("secret");
  });
});

describe("audit consumer", () => {
  it("writes one content-addressed NDJSON object per batch and acks", async () => {
    const bucket = new MemoryBucket();
    const env = { ...localEnv(keys), AUDIT_BUCKET: bucket.asBinding() } as Env;
    const { batch, state } = fakeBatch([event("b"), event("a"), event("c")]);
    await handleAuditBatch(batch, env);
    expect(state).toEqual({ acked: true, retried: false, delay: null });
    const [key, body] = [...bucket.objects.entries()][0] ?? ["", ""];
    expect(key).toMatch(BATCH_KEY);
    expect(key).toBe(await batchKey([event("a"), event("b"), event("c")]));
    expect(body.trim().split("\n").map((l) => (JSON.parse(l) as AuditEvent).request_id)).toEqual(["a", "b", "c"]);
  });

  it("a redelivered batch (any order) gives the same key and identical bytes", async () => {
    const one = new MemoryBucket();
    const two = new MemoryBucket();
    await handleAuditBatch(fakeBatch([event("x1"), event("x2"), event("x3")]).batch, { ...localEnv(keys), AUDIT_BUCKET: one.asBinding() } as Env);
    await handleAuditBatch(fakeBatch([event("x3"), event("x1"), event("x2")]).batch, { ...localEnv(keys), AUDIT_BUCKET: two.asBinding() } as Env);
    expect([...two.objects.entries()]).toEqual([...one.objects.entries()]);
    expect(toNdjson([event("x2"), event("x1")])).toBe(toNdjson([event("x1"), event("x2")]));
  });

  it("retries the whole batch after 60 s when staging fails, and when no store is bound", async () => {
    const bucket = new MemoryBucket();
    bucket.failures = 1;
    const env = { ...localEnv(keys), AUDIT_BUCKET: bucket.asBinding() } as Env;
    const failed = fakeBatch([event("f1")]);
    await handleAuditBatch(failed.batch, env);
    expect(failed.state).toEqual({ acked: false, retried: true, delay: RETRY_DELAY_S });
    expect(RETRY_DELAY_S).toBe(60);
    const again = fakeBatch([event("f1")]);
    await handleAuditBatch(again.batch, env);
    expect(again.state.acked).toBe(true);
    const unbound = fakeBatch([event("f2")]);
    await handleAuditBatch(unbound.batch, { ...localEnv(keys), ENVIRONMENT: "production" } as Env);
    expect(unbound.state).toEqual({ acked: false, retried: true, delay: RETRY_DELAY_S });
  });

  it("locally the consumer stages through the sidecar's /v1/audit/stage", async () => {
    const posts: { url: string; auth: string | null; body: { key: string; body: string } }[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      posts.push({ url: String(input), auth: new Headers(init?.headers).get("authorization"), body: JSON.parse(String(init?.body)) });
      return Response.json({ stored: true });
    };
    const ok = fakeBatch([event("l1")]);
    await handleAuditBatch(ok.batch, localEnv(keys) as Env, fetcher);
    expect(ok.state.acked).toBe(true);
    expect(posts[0]).toMatchObject({ url: "http://127.0.0.1:9/v1/audit/stage", auth: "Bearer test-warehouse-token" });
    expect(posts[0]?.body.key).toMatch(BATCH_KEY);
    const bad = fakeBatch([event("l2")]);
    await handleAuditBatch(bad.batch, localEnv(keys) as Env, async () => new Response("no", { status: 500 }));
    expect(bad.state).toMatchObject({ acked: false, retried: true });
    await expect(sidecarStore("http://127.0.0.1:9", "t", async () => new Response("x", { status: 409 })).put("k", "b")).rejects.toThrow("409");
  });

  it("a batch with nothing that passes the whitelist is acked and dropped", async () => {
    const bucket = new MemoryBucket();
    const { batch, state } = fakeBatch([{ nonsense: true }, "text"]);
    await handleAuditBatch(batch, { ...localEnv(keys), AUDIT_BUCKET: bucket.asBinding() } as Env);
    expect(state.acked).toBe(true);
    expect(bucket.objects.size).toBe(0);
  });
});

describe("audit keys and events", () => {
  it("keys follow the two staging patterns the committer accepts", async () => {
    expect(await directKey(event("0b9c1f9e-7a3b-4f00-9c1e-2b4d6f8a0c11"))).toMatch(DIRECT_KEY);
    expect(await directKey(event("weird id/../x"))).toMatch(DIRECT_KEY);
    expect(await batchKey([event("a")])).toMatch(BATCH_KEY);
    expect(await batchKey([event("a", { ts: "garbage" })], Date.UTC(2026, 0, 2))).toMatch(/dt=2026-01-02\//);
  });

  it("events are built from the response only", async () => {
    const built = await buildAuditEvent(
      {
        request_id: "r9",
        type: "refusal",
        refusal: { code: "PII_REQUEST", message: "x", alternatives: [] },
        meta: {
          latency_ms: 3, llm_ms: 0, warehouse_ms: 0, estimated_bytes: 0, billed_bytes: null, catalog_key: null,
          warehouse: "fake", llm_provider: "stub", model: "m", gateway_log_id: null, decision: "PII_REQUEST", probes: 0, planner_attempts: 0, llm_usage: null,
        },
      },
      { subject: "email:a@x.example", kind: "user", role: "people_analytics", scope: { departments: "all" } },
      "key",
      Date.UTC(2026, 9, 10),
    );
    expect(built).toMatchObject({ action: "refusal", decision: "PII_REQUEST", metric: null, rows: null, ts: "2026-10-10T00:00:00.000Z" });
  });
});
