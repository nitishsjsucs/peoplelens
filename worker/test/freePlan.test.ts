// Workers Free plan bounds inside one invocation (SPEC.md 25.4 D and section 13):
// at most 19 external subrequests per question (the Free limit is 50), one dry run
// per catalog key, and the planner prefix built once per isolate.
import { env as baseEnv } from "cloudflare:workers";
import { exportPKCS8, generateKeyPair } from "jose";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { CatalogFile } from "../src/catalog/catalog";
import type { Env } from "../src/env";
import bqJson from "../src/generated/catalog.bigquery.json";
import { createApp } from "../src/index";
import { PLANNER_PREFIX, buildMessages, plannerPrefix, prefixBuildCount, FEWSHOT } from "../src/planner/prompt";
import { formatValue } from "../src/answer/narrative";
import { POLICY } from "../src/policy/policy";
import { BigQueryWarehouse, type DryRunEntry, MAX_POLLS_PER_QUERY, clearDryRunCache } from "../src/warehouse/bigquery";
import { bindQuery } from "../src/catalog/bind";
import { catalogKey } from "../src/catalog/catalog";
import { validateIntent } from "../src/policy/validate";
import type { GoogleTokenSource } from "../src/warehouse/googleAuth";
import { RecordingLlm } from "./helpers/app";
import { plan as planJson } from "./helpers/fakeAi";
import { HRBP } from "./helpers/principals";
import { LOCAL_BINDINGS, type TestKeys, makeKeys, mintToken, userClaims } from "./helpers/keys";

const BQ = bqJson as unknown as CatalogFile;
const FREE = POLICY.limits.free_plan;
const PROJECT = "peoplelens-fp0001";
const HRBP_EMAIL = "hrbp@example.test";

let keys: TestKeys;
let saKeyJson: string;
beforeAll(async () => {
  keys = await makeKeys();
  const { privateKey } = await generateKeyPair("RS256", { extractable: true });
  saKeyJson = JSON.stringify({
    type: "service_account",
    client_email: "peoplelens-worker@peoplelens-fp0001.iam.gserviceaccount.com",
    private_key: await exportPKCS8(privateKey),
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
});

type Kind = "jwks" | "token" | "dry_run" | "jobs.query" | "poll" | "other";

/** Records every outbound fetch; jobs never complete when `complete` is false. */
function outbound(team: string, opts: { complete: boolean }) {
  const kinds: Kind[] = [];
  let job = 0;
  const fetcher = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input instanceof Request ? input.url : input);
    if (url === `https://${team}/cdn-cgi/access/certs`) {
      kinds.push("jwks");
      return Response.json({ keys: [keys.publicJwk] });
    }
    if (url === "https://oauth2.googleapis.com/token") {
      kinds.push("token");
      return Response.json({ access_token: "ya29.free-plan", expires_in: 3600 });
    }
    if (url.endsWith(`/projects/${PROJECT}/queries`) && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (body["dryRun"]) {
        kinds.push("dry_run");
        return Response.json({ totalBytesProcessed: "10485760" });
      }
      kinds.push("jobs.query");
      job += 1;
      if (!opts.complete) {
        return Response.json({ jobComplete: false, jobReference: { projectId: PROJECT, jobId: `job_${job}`, location: "US" } });
      }
      return Response.json({
        jobComplete: true,
        schema: { fields: [{ name: "headcount", type: "INTEGER" }] },
        rows: [{ f: [{ v: "640" }] }],
        totalBytesBilled: "10485760",
      });
    }
    if (url.includes(`/projects/${PROJECT}/queries/job_`)) {
      kinds.push("poll");
      const jobId = url.split("/queries/")[1]?.split("?")[0];
      return Response.json({ jobComplete: false, jobReference: { projectId: PROJECT, jobId, location: "US" } });
    }
    kinds.push("other");
    return new Response("unexpected", { status: 599 });
  };
  return { kinds, fetcher };
}

function prodEnv(team: string): Env {
  const base = baseEnv as unknown as Env;
  return {
    USER_DO: base.USER_DO,
    ACCOUNT_BUDGET: base.ACCOUNT_BUDGET,
    AI: { run: async () => new Response("{}") } as unknown as Ai,
    ENVIRONMENT: "production",
    LLM_PROVIDER: "workers_ai",
    WAREHOUSE: "bigquery",
    PLANNER_MODEL: "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    GCP_PROJECT_ID: PROJECT,
    GCP_SA_KEY_JSON: saKeyJson,
    AI_GATEWAY_ID: "peoplelens",
    ACCESS_TEAM_DOMAIN: team,
    ACCESS_AUD: "aud-tag",
    ROLE_BINDINGS_JSON: JSON.stringify(LOCAL_BINDINGS),
  } as Env;
}

const count = (kinds: Kind[], k: Kind) => kinds.filter((x) => x === k).length;
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

describe("external subrequests per question", () => {
  it("a restricted question with 3 probes, cold caches and jobs that never complete makes at most 19 fetches", async () => {
    const team = "fp-cold.cloudflareaccess.com";
    const { kinds, fetcher } = outbound(team, { complete: false });
    vi.stubGlobal("fetch", fetcher);
    const env = prodEnv(team);
    // A question that names a location, asked by the HRBP (scope Engineering and Sales):
    // two restricted dimensions, so three complement probes run beside the main query.
    const llm = new RecordingLlm(undefined, [planJson({ metric: "headcount", filters: [{ dimension: "location", values: ["San Jose"] }] })]);
    const app = createApp({ catalog: BQ, makeLlm: () => llm });
    const user = env.USER_DO.get(env.USER_DO.idFromName(`email:${HRBP_EMAIL}`));
    const sessionId = await user.createSession(Date.now());
    const token = await mintToken(keys, { iss: `https://${team}`, aud: ["aud-tag"], claims: userClaims(HRBP_EMAIL) });
    const res = await app.fetch(
      new Request("https://peoplelens.example.workers.dev/api/ask", {
        method: "POST",
        headers: { "Cf-Access-Jwt-Assertion": token, "content-type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: "headcount in San Jose" }),
      }),
      env,
    );
    await settle();
    expect(res.status).toBe(504);
    expect(count(kinds, "jwks")).toBe(1);
    expect(count(kinds, "token")).toBe(1);
    expect(count(kinds, "dry_run")).toBe(1);
    expect(count(kinds, "jobs.query")).toBe(4);
    expect(count(kinds, "poll")).toBe(4 * MAX_POLLS_PER_QUERY);
    expect(count(kinds, "other")).toBe(0);
    expect(kinds.length).toBe(FREE.max_external_subrequests);
    expect(FREE.max_external_subrequests).toBe(19);
    expect(FREE.max_external_subrequests).toBeLessThanOrEqual(50);
  });

  it("a second question with the same catalog key makes no dry run, JWKS or token fetch", async () => {
    const team = "fp-warm.cloudflareaccess.com";
    clearDryRunCache(); // the previous test dry-ran the same catalog key in this isolate
    const { kinds, fetcher } = outbound(team, { complete: true });
    vi.stubGlobal("fetch", fetcher);
    const env = prodEnv(team);
    const scripted = () => planJson({ metric: "headcount", filters: [{ dimension: "location", values: ["Austin"] }] });
    const llm = new RecordingLlm(undefined, [scripted(), scripted()]);
    const app = createApp({ catalog: BQ, makeLlm: () => llm });
    const user = env.USER_DO.get(env.USER_DO.idFromName(`email:${HRBP_EMAIL}`));
    const token = await mintToken(keys, { iss: `https://${team}`, aud: ["aud-tag"], claims: userClaims(HRBP_EMAIL) });
    const ask = async () => {
      const sessionId = await user.createSession(Date.now());
      return app.fetch(
        new Request("https://peoplelens.example.workers.dev/api/ask", {
          method: "POST",
          headers: { "Cf-Access-Jwt-Assertion": token, "content-type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, message: "headcount in Austin" }),
        }),
        env,
      );
    };
    expect((await ask()).status).toBe(200);
    const first = kinds.length;
    expect(count(kinds, "dry_run")).toBe(1);
    expect(count(kinds, "jobs.query")).toBe(4);
    expect((await ask()).status).toBe(200);
    const second = kinds.slice(first);
    expect(second).toEqual(["jobs.query", "jobs.query", "jobs.query", "jobs.query"]);
  });

  it("the main query and its probes share one dry run (parallel calls, one request)", async () => {
    const calls: string[] = [];
    const fetcher: typeof fetch = async () => {
      calls.push("dry_run");
      return Response.json({ totalBytesProcessed: "123" });
    };
    const tokens = { token: async () => "ya29.x" } as unknown as GoogleTokenSource;
    const cache = new Map<string, DryRunEntry>();
    const wh = new BigQueryWarehouse({ projectId: PROJECT, tokens, fetcher, dryRunCache: cache });
    const r = validateIntent(
      { metric: "headcount", groupBy: [], grain: "none", timeRange: { start: "2025-12-01", end: "2025-12-31" }, filters: [{ dimension: "location", values: ["London"] }] },
      HRBP,
    );
    if (!r.ok) throw new Error("invalid intent");
    const q = bindQuery(BQ.entries[catalogKey(r.value)]!, r.value, "bigquery", PROJECT);
    const results = await Promise.all([wh.dryRun(q), wh.dryRun(q), wh.dryRun(q), wh.dryRun(q)]);
    expect(results.map((x) => x.estimatedBytes)).toEqual([123, 123, 123, 123]);
    expect(calls).toHaveLength(1);
    // A failed dry run is not cached.
    const failing = new BigQueryWarehouse({
      projectId: PROJECT,
      tokens,
      fetcher: async () => Response.json({ error: { code: 500, message: "boom" } }, { status: 500 }),
      dryRunCache: new Map(),
    });
    await expect(failing.dryRun(q)).rejects.toMatchObject({ status: 502 });
  });
});

describe("planner prompt CPU structure", () => {
  it("the system prompt and few-shot prefix are built once per isolate; only the request message is per request", () => {
    const builds = prefixBuildCount();
    expect(builds).toBeGreaterThanOrEqual(1);
    expect(plannerPrefix()).toBe(PLANNER_PREFIX);
    expect(PLANNER_PREFIX).toHaveLength(1 + 2 * FEWSHOT.length);
    expect(FEWSHOT).toHaveLength(12);
    expect(Object.isFrozen(PLANNER_PREFIX)).toBe(true);
    const ctx = (q: string) => ({ question: q, principal: HRBP, timeHint: null, values: [], previous: null });
    for (let i = 0; i < 100; i++) {
      const m = buildMessages(ctx(`headcount question ${i}`));
      expect(m[0]).toBe(PLANNER_PREFIX[0]);
      expect(m[m.length - 2]).toBe(PLANNER_PREFIX[PLANNER_PREFIX.length - 1]);
      expect(m[m.length - 1]?.content).toContain(`headcount question ${i}`);
    }
    expect(prefixBuildCount()).toBe(builds);
  });
});

describe("narrative number formatting built at startup", () => {
  it("formats exactly as toLocaleString did, for every unit", () => {
    for (const v of [0, -0.4, 7, 8.5, 999.5, 1000, 1724, 1780, 61234.56, 127300, 1234567.89]) {
      expect(formatValue("employees", v)).toBe(Math.round(v).toLocaleString("en-US"));
      expect(formatValue("USD", v)).toBe(`$${Math.round(v).toLocaleString("en-US")}`);
      expect(formatValue("FTE", v)).toBe(v.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }));
    }
    expect(formatValue("employees", null)).toBe("hidden");
  });
});
