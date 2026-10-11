// CPU per /api/ask on the production request path, measured in Node (SPEC.md 25.4 D
// and 25.5). Not a CI gate: timings depend on the machine. Run it with
// `npm run bench:cpu --workspace worker` (bench/run.mjs bundles this file with the
// esbuild that wrangler already pins, then runs it in fresh Node processes).
//
// What runs is the production wiring itself: createApp with the BigQuery catalog,
// ENVIRONMENT=production, defaultLlm (WorkersAiClient with the AccountBudgetDO port)
// and defaultWarehouse (BigQueryWarehouse with the Google service-account token
// source), Access JWT verification against a remote JWKS, the planner prefix built at
// startup, suppression and the narrative. Only the far side of every boundary is
// faked, and its answers are recorded before any measurement: the network (team
// JWKS, Google token, BigQuery REST bodies that the Worker still parses and decodes),
// the Workers AI binding (it serializes the inputs, as the real binding does, and
// answers with the stub planner's recorded output and typical token counts), and the
// Durable Objects (in-memory, always granting). Worker logs are stringified as in
// production, then dropped. The audit trail is off.
//
// This file uses only Web APIs; bench/launch.mjs supplies the CPU clock and files.
import bqJson from "../src/generated/catalog.bigquery.json";
import type { CatalogFile } from "../src/catalog/catalog";
import type { Env } from "../src/env";
import { createApp } from "../src/index";
import { StubPlanner } from "../src/llm/stub";
import type { LlmClient, PlanCall, PlannerInput } from "../src/llm/types";
import { BigQueryWarehouse } from "../src/warehouse/bigquery";
import { GOOGLE_TOKEN_URL, GoogleTokenSource, parseServiceAccountKey } from "../src/warehouse/googleAuth";
import type { CellValue, RunOptions, RunResult, Warehouse } from "../src/warehouse/types";
import type { BoundQuery } from "../src/catalog/bind";
import { FakeWarehouse } from "../test/helpers/fakeWarehouse";
import { MemoryUser } from "../test/helpers/memoryUser";
import { LOCAL_BINDINGS, ROLE_EMAILS } from "../test/helpers/keys";

const BQ = bqJson as unknown as CatalogFile;
export const TEAM = "bench.cloudflareaccess.com";
export const AUD = "bench-aud";
export const PROJECT = "peoplelens-bench1";
const ORIGIN = "https://peoplelens.bench.workers.dev";
const JWKS_URL = `https://${TEAM}/cdn-cgi/access/certs`;
const MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";
/** Typical planner usage from SPEC.md 25.5 (3,600 tokens in, 67 out). */
const USAGE = { prompt_tokens: 3600, completion_tokens: 67, total_tokens: 3667 };
const DRY_RUN_TEXT = JSON.stringify({ totalBytesProcessed: "10485760" });
const TOKEN_TEXT = JSON.stringify({ access_token: "ya29.bench", expires_in: 3600 });

export interface EvalItem {
  id: string;
  role: keyof typeof ROLE_EMAILS;
  turns: { text: string }[];
}

/** Everything a fresh process needs, prepared and recorded before measuring. */
export interface Memo {
  jwks: unknown;
  saKeyJson: string;
  tokens: Record<string, string>;
  /** Workers AI response bodies by the request's final user message. */
  llm: Record<string, string>;
  /** BigQuery jobs.query response bodies by the exact request body. */
  bq: Record<string, string>;
}

export interface Samples {
  asks: number[];
  answers: number;
  /** CPU of createApp: in production it runs at module evaluation (startup), including the warm-up pass. */
  appMs?: number;
}

const BIGQUERY_TYPES: Record<string, string> = { STRING: "STRING", TIMESTAMP: "TIMESTAMP", INTEGER: "INTEGER", FLOAT: "FLOAT" };

/** A fake warehouse result in BigQuery REST form, as jobs.query would return it. */
function restBody(q: BoundQuery, res: RunResult): string {
  const entry = q.entry;
  const dimCols = new Set(entry.group_by.map((d) => entry.columns.dimensions[d] ?? d));
  const fields = res.columns.map((name) => ({
    name,
    type:
      BIGQUERY_TYPES[
        dimCols.has(name) ? "STRING" : name === entry.columns.time ? "TIMESTAMP" : name === entry.columns.cohort || name === "headcount" ? "INTEGER" : "FLOAT"
      ],
  }));
  const cell = (v: CellValue, type: string | undefined) => {
    if (v === null) return { v: null };
    if (type === "TIMESTAMP") return { v: String(Date.parse(`${String(v)}T00:00:00Z`) * 1000) };
    return { v: String(v) };
  };
  return JSON.stringify({
    jobComplete: true,
    schema: { fields },
    rows: res.rows.map((r) => ({ f: r.map((v, i) => cell(v, fields[i]?.type)) })),
    totalBytesProcessed: "10485760",
    totalBytesBilled: "10485760",
  });
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
}

/** The network the production Worker talks to, answered from the memo. */
function network(memo: Memo, record?: (body: string) => string): typeof fetch {
  const jwksText = JSON.stringify(memo.jwks);
  return async (input, init) => {
    const url = urlOf(input);
    if (url === JWKS_URL) return new Response(jwksText, { headers: { "content-type": "application/json" } });
    if (url === GOOGLE_TOKEN_URL) return new Response(TOKEN_TEXT, { headers: { "content-type": "application/json" } });
    const body = typeof init?.body === "string" ? init.body : "";
    if (body.includes('"dryRun":true')) return new Response(DRY_RUN_TEXT);
    const text = record ? record(body) : memo.bq[body];
    if (text === undefined) throw new Error(`bench: no recorded BigQuery answer for ${url}`);
    return new Response(text);
  };
}

function aiBinding(memo: Memo): Ai {
  return {
    async run(_model: string, inputs: { messages: { content: string }[] }, options: unknown) {
      // The real binding serializes the request for its HTTP call to the gateway.
      JSON.stringify({ inputs, options });
      const key = inputs.messages[inputs.messages.length - 1]?.content ?? "";
      const text = memo.llm[key];
      if (text === undefined) throw new Error("bench: no recorded planner answer");
      return new Response(text, { headers: { "content-type": "application/json", "cf-aig-log-id": "bench-log" } });
    },
  } as unknown as Ai;
}

function namespace<T>(make: (name: string) => T): DurableObjectNamespace {
  const objects = new Map<string, T>();
  return {
    idFromName: (name: string) => name,
    get: (id: string) => {
      let o = objects.get(id);
      if (!o) {
        o = make(id);
        objects.set(id, o);
      }
      return o;
    },
  } as unknown as DurableObjectNamespace;
}

const alwaysGrant = {
  reserve: async (_day: string, _counter: string, amount: number, cap: number) => ({ ok: true, used: amount, cap }),
  settle: async () => undefined,
  usage: async () => ({ neurons: 0, audit_queue_messages: 0 }),
};

function productionEnv(memo: Memo): Env {
  return {
    USER_DO: namespace(() => new MemoryUser()),
    ACCOUNT_BUDGET: namespace(() => alwaysGrant),
    AI: aiBinding(memo),
    ENVIRONMENT: "production",
    LLM_PROVIDER: "workers_ai",
    WAREHOUSE: "bigquery",
    PLANNER_MODEL: MODEL,
    GCP_PROJECT_ID: PROJECT,
    GCP_SA_KEY_JSON: memo.saKeyJson,
    AI_GATEWAY_ID: "peoplelens",
    ACCESS_TEAM_DOMAIN: TEAM,
    ACCESS_AUD: AUD,
    ROLE_BINDINGS_JSON: JSON.stringify(LOCAL_BINDINGS),
  } as unknown as Env;
}

const ctx = { waitUntil() {}, passThroughOnException() {}, props: {} } as unknown as ExecutionContext;

/** A virtual clock: 3 s per ask keeps each identity under the 30-per-minute rate limit,
 * and each pass starts a new UTC day, so the per-user daily byte budget resets. */
const START = Date.UTC(2026, 9, 1, 0, 0, 0);
function virtualClock() {
  const clock = { t: START };
  return { now: () => clock.t, tick: () => (clock.t += 3000), day: (pass: number) => (clock.t = START + pass * 86_400_000) };
}

interface Runner {
  ask(item: EvalItem, turn: number, sessionId: string): Promise<{ status: number; type: string | null }>;
  session(item: EvalItem): Promise<string>;
}

function runner(memo: Memo, app: ReturnType<typeof createApp>, env: Env, clock: ReturnType<typeof virtualClock>): Runner {
  return {
    async session(item) {
      const user = (env.USER_DO as unknown as { get(id: string): MemoryUser }).get(`email:${ROLE_EMAILS[item.role]}`);
      return user.createSession(clock.now());
    },
    async ask(item, turn, sessionId) {
      clock.tick();
      const res = await app.fetch(
        new Request(`${ORIGIN}/api/ask`, {
          method: "POST",
          headers: { "Cf-Access-Jwt-Assertion": memo.tokens[item.role] ?? "", "content-type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, message: item.turns[turn]?.text ?? "" }),
        }),
        env,
        ctx,
      );
      const body = (await res.json()) as { type?: string };
      return { status: res.status, type: body.type ?? null };
    },
  };
}

/**
 * Recording pass (never measured): the same requests with the stub planner and the
 * fake warehouse behind recorders, so the measured passes can answer from the memo.
 */
export async function record(items: EvalItem[], memo: Memo): Promise<void> {
  const stub = new StubPlanner();
  const recorder: LlmClient = {
    provider: "workers_ai",
    model: MODEL,
    async plan(input: PlannerInput): Promise<PlanCall> {
      const call = await stub.plan(input);
      const key = input.messages[input.messages.length - 1]?.content ?? "";
      memo.llm[key] = JSON.stringify({ response: call.raw, usage: USAGE });
      return { raw: call.raw, ms: 0, usage: USAGE };
    },
  };
  const fake = new FakeWarehouse();
  const env = productionEnv(memo);
  globalThis.fetch = network(memo);
  const recordingWarehouse = (): Warehouse => ({
    kind: "bigquery",
    dryRun: async () => ({ estimatedBytes: 10485760 }),
    async run(q: BoundQuery, o: RunOptions) {
      const text = restBody(q, await fake.run(q, o));
      const real = new BigQueryWarehouse({
        projectId: PROJECT,
        tokens: new GoogleTokenSource(parseServiceAccountKey(memo.saKeyJson), network(memo)),
        fetcher: network(memo, (body) => {
          memo.bq[body] = text;
          return text;
        }),
      });
      return real.run(q, o);
    },
  });
  const clock = virtualClock();
  const app = createApp({ catalog: BQ, makeLlm: () => recorder, makeWarehouse: recordingWarehouse, now: clock.now });
  const r = runner(memo, app, env, clock);
  for (const item of items) {
    const s = await r.session(item);
    for (let t = 0; t < item.turns.length; t++) await r.ask(item, t, s);
  }
}

/** Measured passes over every turn with the production wiring; returns CPU ms per ask. */
export async function measure(items: EvalItem[], memo: Memo, cpuMs: () => number, passes: number, warmup = 1): Promise<Samples> {
  globalThis.fetch = network(memo);
  const env = productionEnv(memo);
  const clock = virtualClock();
  const appStart = cpuMs();
  const app = createApp({ catalog: BQ, now: clock.now });
  const appMs = cpuMs() - appStart;
  const r = runner(memo, app, env, clock);
  const out: Samples = { asks: [], answers: 0, appMs };
  for (let pass = 0; pass < warmup + passes; pass++) {
    clock.day(pass);
    for (const item of items) {
      const s = await r.session(item);
      for (let t = 0; t < item.turns.length; t++) {
        const before = cpuMs();
        const res = await r.ask(item, t, s);
        const spent = cpuMs() - before;
        if (res.status !== 200) throw new Error(`bench: ${item.id} turn ${t} answered HTTP ${res.status}`);
        if (pass >= warmup) {
          out.asks.push(spent);
          if (res.type === "answer") out.answers += 1;
        }
      }
    }
  }
  return out;
}

/** The first `n` asks of a fresh isolate, each in its own session. */
export async function measureCold(items: EvalItem[], memo: Memo, cpuMs: () => number, n: number): Promise<Samples> {
  return measure(items.slice(0, n).map((i) => ({ ...i, turns: i.turns.slice(0, 1) })), memo, cpuMs, 1, 0);
}
