import { beforeAll, describe, expect, it } from "vitest";
import { AUDIT_FIELDS, buildAuditEvent, cleanAuditRecord, forwardAuditBatch, subjectHmac } from "../src/audit/audit";
import type { Env } from "../src/env";
import { createApp, handleAuditBatch } from "../src/index";
import { DUCK_CATALOG, RecordingLlm, ORIGIN } from "./helpers/app";
import { localEnv } from "./helpers/env";
import { FakeWarehouse } from "./helpers/fakeWarehouse";
import { ROLE_EMAILS, type TestKeys, makeKeys, mintToken, userClaims } from "./helpers/keys";

let keys: TestKeys;
beforeAll(async () => {
  keys = await makeKeys();
});

const MARKER = "OKAPI-MARKER-5521";

function fakeBatch(bodies: unknown[]) {
  const state = { acked: false, retried: false };
  const batch = {
    queue: "peoplelens-audit",
    messages: bodies.map((body, i) => ({ id: String(i), timestamp: new Date(), body, attempts: 1, ack() {}, retry() {} })),
    ackAll() {
      state.acked = true;
    },
    retryAll() {
      state.retried = true;
    },
  } as unknown as MessageBatch<unknown>;
  return { batch, state };
}

async function askOnce(env: Env): Promise<void> {
  const app = createApp({ catalog: DUCK_CATALOG, makeWarehouse: () => new FakeWarehouse(), makeLlm: () => new RecordingLlm() });
  const token = await mintToken(keys, { claims: userClaims(ROLE_EMAILS.executive) });
  const headers = { "Cf-Access-Jwt-Assertion": token, "content-type": "application/json" };
  const s = await app.fetch(new Request(`${ORIGIN}/api/sessions`, { method: "POST", headers, body: "{}" }), env);
  const sessionId = ((await s.json()) as { session_id: string }).session_id;
  await app.fetch(new Request(`${ORIGIN}/api/ask`, { method: "POST", headers, body: JSON.stringify({ session_id: sessionId, message: `${MARKER} headcount by location` }) }), env);
}

describe("audit trail", () => {
  it("sends one metadata-only event per question when the queue and key are configured", async () => {
    const sent: unknown[] = [];
    const env = localEnv(keys, { AUDIT_HMAC_KEY: "k".repeat(32) }) as Env;
    env.AUDIT_QUEUE = { send: async (m: unknown) => void sent.push(m) } as unknown as Queue<unknown>;
    await askOnce(env);
    expect(sent).toHaveLength(1);
    const event = sent[0] as Record<string, unknown>;
    expect(Object.keys(event).sort()).toEqual([...AUDIT_FIELDS].sort());
    expect(event).toMatchObject({ role: "executive", action: "answer", metric: "headcount", group_by: "location", catalog_key: "headcount|location|none", rows: 5 });
    const text = JSON.stringify(event);
    expect(text).not.toContain(MARKER);
    expect(text).not.toContain("exec@example.test");
    expect(event["subject_hmac"]).toBe(await subjectHmac("k".repeat(32), "email:exec@example.test"));
    expect(String(event["subject_hmac"])).toMatch(/^[0-9a-f]{64}$/);
  });

  it("sends nothing without the HMAC key", async () => {
    const sent: unknown[] = [];
    const env = localEnv(keys) as Env;
    env.AUDIT_QUEUE = { send: async (m: unknown) => void sent.push(m) } as unknown as Queue<unknown>;
    await askOnce(env);
    expect(sent).toEqual([]);
  });

  it("strips anything outside the whitelist before forwarding", () => {
    const r = cleanAuditRecord({ ts: "t", request_id: "r", question: "secret", role: "executive", rows: 3, rogue: { a: 1 } });
    expect(r).not.toBeNull();
    expect(Object.keys(r ?? {})).toEqual([...AUDIT_FIELDS]);
    expect(JSON.stringify(r)).not.toContain("secret");
    expect(cleanAuditRecord({ role: "x" })).toBeNull();
  });

  it("forwards batches to the Basin stream in production and to the sidecar locally", async () => {
    const streamed: unknown[][] = [];
    const record = { ts: "2026-10-10T00:00:00Z", request_id: "r1", role: "executive" };
    expect(await forwardAuditBatch([record], { stream: { send: async (r) => void streamed.push(r) } })).toBe("stream");
    expect(streamed[0]).toHaveLength(1);
    const posts: { url: string; auth: string | null; body: string }[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      posts.push({ url: String(input), auth: new Headers(init?.headers).get("authorization"), body: String(init?.body) });
      return Response.json({ appended: 1 });
    };
    expect(await forwardAuditBatch([record], { localUrl: "http://127.0.0.1:5555", localToken: "tok", fetcher })).toBe("local");
    expect(posts[0]).toMatchObject({ url: "http://127.0.0.1:5555/v1/audit", auth: "Bearer tok" });
    expect(await forwardAuditBatch([record], {})).toBe("dropped");
  });

  it("the queue consumer acks on success and retries on failure", async () => {
    const env = localEnv(keys) as Env;
    const ok = fakeBatch([{ ts: "t", request_id: "r" }]);
    await handleAuditBatch(ok.batch, env, async () => Response.json({ appended: 1 }));
    expect(ok.state).toEqual({ acked: true, retried: false });
    const bad = fakeBatch([{ ts: "t", request_id: "r" }]);
    await handleAuditBatch(bad.batch, env, async () => new Response("no", { status: 500 }));
    expect(bad.state).toEqual({ acked: false, retried: true });
    const prod = fakeBatch([{ ts: "t", request_id: "r" }]);
    await handleAuditBatch(prod.batch, { ...env, ENVIRONMENT: "production" } as Env);
    expect(prod.state.acked).toBe(true);
  });

  it("events are built from the response only", async () => {
    const event = await buildAuditEvent(
      {
        request_id: "r9",
        type: "refusal",
        refusal: { code: "PII_REQUEST", message: "x", alternatives: [] },
        meta: {
          latency_ms: 3, llm_ms: 0, warehouse_ms: 0, estimated_bytes: 0, billed_bytes: null, catalog_key: null,
          warehouse: "fake", llm_provider: "stub", model: "m", gateway_log_id: null, decision: "PII_REQUEST", probes: 0, planner_attempts: 0,
        },
      },
      { subject: "email:a@x.example", kind: "user", role: "people_analytics", scope: { departments: "all" } },
      "key",
      Date.UTC(2026, 9, 10),
    );
    expect(event).toMatchObject({ action: "refusal", decision: "PII_REQUEST", metric: null, rows: null, ts: "2026-10-10T00:00:00.000Z" });
  });
});
