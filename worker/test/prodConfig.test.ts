import { beforeAll, describe, expect, it } from "vitest";
import type { CatalogFile } from "../src/catalog/catalog";
import { type Env, PRODUCTION_REQUIRED_SECRETS, loadConfig } from "../src/env";
import { createApp } from "../src/index";
import { type EnvOverrides, localEnv } from "./helpers/env";
import { LOCAL_BINDINGS, type TestKeys, makeKeys, mintToken, userClaims } from "./helpers/keys";

const catalog = { header: { entries: 312 }, entries: {} } as unknown as CatalogFile;
const app = createApp({ catalog });
const fakeAi = { run: async () => ({}) } as unknown as Ai;
const PROD_URL = new URL("https://peoplelens.example.workers.dev/api/health");

let keys: TestKeys;
beforeAll(async () => {
  keys = await makeKeys();
});

function prodEnv(overrides: EnvOverrides = {}): Env {
  const env = localEnv(keys);
  const out: Env = {
    USER_DO: env.USER_DO,
    ACCOUNT_BUDGET: env.ACCOUNT_BUDGET,
    AI: fakeAi,
    ENVIRONMENT: "production",
    LLM_PROVIDER: "workers_ai",
    WAREHOUSE: "bigquery",
    PLANNER_MODEL: "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    GCP_PROJECT_ID: "peoplelens-abc123",
    GCP_SA_KEY_JSON: "{}",
    AI_GATEWAY_ID: "peoplelens",
    ACCESS_TEAM_DOMAIN: "lumen.cloudflareaccess.com",
    ACCESS_AUD: "aud-tag",
    ROLE_BINDINGS_JSON: JSON.stringify(LOCAL_BINDINGS),
    ...overrides,
  } as Env;
  return out;
}

describe("production guards", () => {
  it("accepts a complete production configuration with the remote team JWKS", () => {
    const r = loadConfig(prodEnv(), PROD_URL);
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.config.access.jwks).toEqual({ kind: "remote", url: "https://lumen.cloudflareaccess.com/cdn-cgi/access/certs" });
      expect(r.config.access.issuer).toBe("https://lumen.cloudflareaccess.com");
      expect(r.config.llmProvider).toBe("workers_ai");
      expect(r.config.warehouse).toBe("bigquery");
    }
  });

  it("names every missing secret and never its value", () => {
    for (const name of PRODUCTION_REQUIRED_SECRETS) {
      const r = loadConfig(prodEnv({ [name]: undefined }), PROD_URL);
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.missing).toContain(name);
    }
    const env = prodEnv({ AI: undefined });
    const r = loadConfig(env, PROD_URL);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.missing).toContain("AI");
    // Without AccountBudgetDO nothing would cap this repo's share of the account's neurons.
    const noBudget = loadConfig(prodEnv({ ACCOUNT_BUDGET: undefined }), PROD_URL);
    expect(noBudget.ok).toBe(false);
    if (!noBudget.ok) expect(noBudget.missing).toContain("ACCOUNT_BUDGET");
  });

  it("refuses local stand-ins in production", () => {
    const cases: EnvOverrides[] = [
      { ACCESS_JWKS_JSON: keys.jwksJson },
      { LLM_PROVIDER: "stub" },
      { LLM_PROVIDER: "openai_compat" },
      { WAREHOUSE: "duckdb_local" },
      { WAREHOUSE_URL: "http://127.0.0.1:1234" },
    ];
    for (const c of cases) {
      // even on a loopback host
      expect(loadConfig(prodEnv(c), new URL("http://127.0.0.1/api/health")).ok).toBe(false);
    }
  });

  it("health answers 503 with names only between bootstrap phases A and B", async () => {
    const env = prodEnv({ ACCESS_TEAM_DOMAIN: undefined, ACCESS_AUD: undefined, ROLE_BINDINGS_JSON: undefined });
    const res = await app.fetch(new Request(PROD_URL), env);
    expect(res.status).toBe(503);
    const body = (await res.json()) as { missing: string[] };
    expect(body.missing).toEqual(["ACCESS_AUD", "ACCESS_TEAM_DOMAIN", "ROLE_BINDINGS_JSON"]);
    expect(JSON.stringify(body)).not.toContain("peoplelens-abc123");
  });

  it("every other /api route fails closed with 503 when configuration is incomplete", async () => {
    const env = prodEnv({ GCP_SA_KEY_JSON: undefined });
    for (const path of ["/api/me", "/api/catalog"]) {
      const res = await app.fetch(new Request(new URL(path, PROD_URL)), env);
      expect(res.status).toBe(503);
    }
  });
});

describe("loopback-only test identity", () => {
  it("refuses a static JWKS on a non-loopback host, even locally", () => {
    const env = localEnv(keys);
    expect(loadConfig(env, new URL("http://127.0.0.1:8794/api/me")).ok).toBe(true);
    expect(loadConfig(env, new URL("http://localhost:8794/api/me")).ok).toBe(true);
    const r = loadConfig(env, new URL("https://peoplelens-local.example.workers.dev/api/me"));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.problems.join(" ")).toContain("loopback");
  });

  it("refuses a .cloudflareaccess.test issuer on a non-loopback host", () => {
    const env = localEnv(keys, { ACCESS_JWKS_JSON: undefined, ACCESS_TEAM_DOMAIN: "lumen.cloudflareaccess.com" });
    // a remote JWKS with a .test issuer cannot be configured: the issuer is derived from the team domain
    const r = loadConfig(env, new URL("https://x.workers.dev/api/me"));
    if (r.ok) expect(r.config.access.issuer).toBe("https://lumen.cloudflareaccess.com");
    const r2 = loadConfig(localEnv(keys), new URL("https://x.workers.dev/api/me"));
    expect(r2.ok).toBe(false);
    if (!r2.ok) expect(r2.problems.join(" ")).toContain("cloudflareaccess.test");
  });

  it("a request with a valid local token on a public host is refused before auth", async () => {
    const token = await mintToken(keys, { claims: userClaims("analyst@example.test") });
    const res = await app.fetch(
      new Request("https://peoplelens-local.example.workers.dev/api/me", { headers: { "Cf-Access-Jwt-Assertion": token } }),
      localEnv(keys),
    );
    expect(res.status).toBe(503);
  });

  it("local warehouse and LLM endpoints must be loopback", () => {
    expect(loadConfig(localEnv(keys, { WAREHOUSE_URL: "https://evil.example/" }), new URL("http://127.0.0.1/")).ok).toBe(false);
    expect(
      loadConfig(localEnv(keys, { LLM_PROVIDER: "openai_compat", LLM_BASE_URL: "https://api.example/v1" }), new URL("http://127.0.0.1/")).ok,
    ).toBe(false);
  });
});

describe("build commit in /api/health (multi-day production eval)", () => {
  it("reports the deployed commit, never a malformed value", async () => {
    const { buildCommit } = await import("../src/env");
    const sha = "5867b50a1b2c3d4e5f60718293a4b5c6d7e8f901";
    expect(buildCommit({ BUILD_COMMIT: sha } as Env)).toBe(sha);
    expect(buildCommit({ BUILD_COMMIT: `${sha.slice(0, 7)}-dirty` } as Env)).toBe("5867b50-dirty");
    expect(buildCommit({ BUILD_COMMIT: "<script>" } as Env)).toBeNull();
    expect(buildCommit({} as Env)).toBeNull();
    const res = await app.fetch(new Request(PROD_URL), prodEnv({ BUILD_COMMIT: sha }));
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ ok: true, build_commit: sha, planner_model: "@cf/meta/llama-3.3-70b-instruct-fp8-fast" });
  });
});
