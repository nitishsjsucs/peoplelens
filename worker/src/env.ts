// Hand-written Env (no `wrangler types` output is committed: it includes runtime
// types by default and reads .dev.vars, so it differs between machines).
// test/bindings.test.ts parses wrangler.jsonc and asserts the names match.
import type { JSONWebKeySet } from "jose";
import { type JWKSource, teamCertsUrl, teamIssuer } from "./auth/access";
import { type RoleBindings, parseRoleBindings } from "./auth/principal";
import { ApiError } from "./policy/types";
import type { AccountBudgetDO } from "./session/accountBudgetDO";
import type { UserDO } from "./session/userDO";

export interface Env {
  // bindings
  USER_DO: DurableObjectNamespace<UserDO>;
  /** This repo's share of the account's daily Free allowances (one instance). */
  ACCOUNT_BUDGET: DurableObjectNamespace<AccountBudgetDO>;
  AI?: Ai;
  // optional metadata-only audit trail (below the core cut line): the queue, and the
  // R2 staging bucket in production (added by the generated --with-audit config)
  AUDIT_QUEUE?: Queue<unknown>;
  AUDIT_BUCKET?: R2Bucket;
  AUDIT_HMAC_KEY?: string;
  // vars
  ENVIRONMENT?: string;
  LLM_PROVIDER?: string;
  WAREHOUSE?: string;
  PLANNER_MODEL?: string;
  /** Set by every deploy (`--var BUILD_COMMIT:<git rev-parse HEAD>`, "-dirty" appended for a
   * dirty tree), so a multi-day production eval can check the build never changed. */
  BUILD_COMMIT?: string;
  // local only (.dev.vars written by `pl dev up`)
  ACCESS_JWKS_JSON?: string;
  ACCESS_ISSUER?: string;
  WAREHOUSE_URL?: string;
  WAREHOUSE_TOKEN?: string;
  LLM_BASE_URL?: string;
  // secrets (production; ACCESS_AUD and ROLE_BINDINGS_JSON also locally)
  ACCESS_AUD?: string;
  ACCESS_TEAM_DOMAIN?: string;
  ROLE_BINDINGS_JSON?: string;
  GCP_PROJECT_ID?: string;
  GCP_SA_KEY_JSON?: string;
  AI_GATEWAY_ID?: string;
}

export const ENV_BINDING_NAMES = ["USER_DO", "ACCOUNT_BUDGET", "AI"] as const;
export const ENV_VAR_NAMES = ["ENVIRONMENT", "LLM_PROVIDER", "WAREHOUSE", "PLANNER_MODEL"] as const;
export const PRODUCTION_REQUIRED_SECRETS = [
  "GCP_PROJECT_ID",
  "GCP_SA_KEY_JSON",
  "AI_GATEWAY_ID",
  "ACCESS_TEAM_DOMAIN",
  "ACCESS_AUD",
  "ROLE_BINDINGS_JSON",
] as const;

export type LlmProvider = "workers_ai" | "openai_compat" | "stub";
export type WarehouseKind = "bigquery" | "duckdb_local";

export interface Config {
  environment: "local" | "production";
  llmProvider: LlmProvider;
  warehouse: WarehouseKind;
  plannerModel: string;
  access: { aud: string; issuer: string; jwks: JWKSource };
  roleBindings: RoleBindings;
  warehouseUrl?: string;
  warehouseToken?: string;
  llmBaseUrl?: string;
  gcpProjectId?: string;
  gcpSaKeyJson?: string;
  aiGatewayId?: string;
}

export type ConfigResult = { ok: true; config: Config } | { ok: false; missing: string[]; problems: string[] };

const GCP_PROJECT_RE = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

export function isLoopbackHost(hostname: string): boolean {
  return LOOPBACK.has(hostname.toLowerCase());
}

function present(v: string | undefined): v is string {
  return typeof v === "string" && v.trim() !== "";
}

/**
 * Validate the environment for one request. Fails closed: in production every
 * /api route answers 503 with the names (never values) of what is missing.
 * In every environment a static JWKS or a *.cloudflareaccess.test issuer is
 * refused unless the request host is loopback.
 */
export function loadConfig(env: Env, requestUrl: URL): ConfigResult {
  const missing: string[] = [];
  const problems: string[] = [];
  const environment = env.ENVIRONMENT === "production" ? "production" : "local";
  const loopback = isLoopbackHost(requestUrl.hostname);

  const llm = env.LLM_PROVIDER;
  const wh = env.WAREHOUSE;
  if (llm !== "workers_ai" && llm !== "openai_compat" && llm !== "stub") problems.push("LLM_PROVIDER is invalid");
  if (wh !== "bigquery" && wh !== "duckdb_local") problems.push("WAREHOUSE is invalid");

  if (environment === "production") {
    for (const name of PRODUCTION_REQUIRED_SECRETS) if (!present(env[name])) missing.push(name);
    if (!env.AI) missing.push("AI");
    // Without the budget object nothing would cap this repo's use of the account's neurons.
    if (!env.ACCOUNT_BUDGET) missing.push("ACCOUNT_BUDGET");
    if (present(env.ACCESS_JWKS_JSON)) problems.push("ACCESS_JWKS_JSON must not be set in production");
    if (present(env.ACCESS_ISSUER)) problems.push("ACCESS_ISSUER must not be set in production");
    if (llm !== "workers_ai") problems.push("LLM_PROVIDER must be workers_ai in production");
    if (wh !== "bigquery") problems.push("WAREHOUSE must be bigquery in production");
    if (present(env.WAREHOUSE_URL) || present(env.LLM_BASE_URL)) problems.push("local endpoints must not be set in production");
  }

  // Identity
  let jwks: JWKSource | undefined;
  let issuer: string | undefined;
  if (present(env.ACCESS_JWKS_JSON)) {
    if (!loopback) problems.push("a static JWKS is only accepted on a loopback host");
    try {
      const parsed = JSON.parse(env.ACCESS_JWKS_JSON) as JSONWebKeySet;
      if (!Array.isArray(parsed.keys) || parsed.keys.length === 0) problems.push("ACCESS_JWKS_JSON has no keys");
      jwks = { kind: "static", jwks: parsed };
    } catch {
      problems.push("ACCESS_JWKS_JSON is not valid JSON");
    }
    if (present(env.ACCESS_ISSUER)) issuer = env.ACCESS_ISSUER;
    else missing.push("ACCESS_ISSUER");
  } else if (present(env.ACCESS_TEAM_DOMAIN)) {
    if (!/^[a-z0-9-]+\.cloudflareaccess\.com$/.test(env.ACCESS_TEAM_DOMAIN)) {
      problems.push("ACCESS_TEAM_DOMAIN must look like <team>.cloudflareaccess.com");
    }
    jwks = { kind: "remote", url: teamCertsUrl(env.ACCESS_TEAM_DOMAIN) };
    issuer = teamIssuer(env.ACCESS_TEAM_DOMAIN);
  } else if (environment === "local") {
    missing.push("ACCESS_JWKS_JSON");
  }
  if (issuer && /\.cloudflareaccess\.test$/.test(new URL(issuer).hostname) && !loopback) {
    problems.push("a *.cloudflareaccess.test issuer is only accepted on a loopback host");
  }
  if (!present(env.ACCESS_AUD) && environment === "local") missing.push("ACCESS_AUD");

  let roleBindings: RoleBindings = {};
  if (present(env.ROLE_BINDINGS_JSON)) {
    try {
      roleBindings = parseRoleBindings(env.ROLE_BINDINGS_JSON);
    } catch (err) {
      problems.push(err instanceof ApiError ? err.message : "ROLE_BINDINGS_JSON is invalid");
    }
  } else if (environment === "local") {
    missing.push("ROLE_BINDINGS_JSON");
  }

  // Warehouse
  if (wh === "duckdb_local") {
    if (!present(env.WAREHOUSE_URL)) missing.push("WAREHOUSE_URL");
    else if (!isLoopbackHost(new URL(env.WAREHOUSE_URL).hostname)) problems.push("WAREHOUSE_URL must be a loopback URL");
    if (!present(env.WAREHOUSE_TOKEN)) missing.push("WAREHOUSE_TOKEN");
  }
  if (wh === "bigquery" && environment !== "production") {
    for (const name of ["GCP_PROJECT_ID", "GCP_SA_KEY_JSON"] as const) if (!present(env[name])) missing.push(name);
  }
  if (present(env.GCP_PROJECT_ID) && !GCP_PROJECT_RE.test(env.GCP_PROJECT_ID)) problems.push("GCP_PROJECT_ID is malformed");

  // LLM
  if (llm === "openai_compat") {
    if (!present(env.LLM_BASE_URL)) missing.push("LLM_BASE_URL");
    else if (!isLoopbackHost(new URL(env.LLM_BASE_URL).hostname)) problems.push("LLM_BASE_URL must be a loopback URL");
  }
  if (llm === "workers_ai" && environment !== "production") {
    if (!env.AI) missing.push("AI");
    if (!present(env.AI_GATEWAY_ID)) missing.push("AI_GATEWAY_ID");
  }

  const uniqueMissing = [...new Set(missing)].sort();
  if (uniqueMissing.length > 0 || problems.length > 0 || !jwks || !issuer || !present(env.ACCESS_AUD)) {
    return { ok: false, missing: uniqueMissing, problems };
  }
  const config: Config = {
    environment,
    llmProvider: llm as LlmProvider,
    warehouse: wh as WarehouseKind,
    plannerModel: env.PLANNER_MODEL ?? "",
    access: { aud: env.ACCESS_AUD, issuer, jwks },
    roleBindings,
  };
  if (present(env.WAREHOUSE_URL)) config.warehouseUrl = env.WAREHOUSE_URL;
  if (present(env.WAREHOUSE_TOKEN)) config.warehouseToken = env.WAREHOUSE_TOKEN;
  if (present(env.LLM_BASE_URL)) config.llmBaseUrl = env.LLM_BASE_URL;
  if (present(env.GCP_PROJECT_ID)) config.gcpProjectId = env.GCP_PROJECT_ID;
  if (present(env.GCP_SA_KEY_JSON)) config.gcpSaKeyJson = env.GCP_SA_KEY_JSON;
  if (present(env.AI_GATEWAY_ID)) config.aiGatewayId = env.AI_GATEWAY_ID;
  return { ok: true, config };
}

const BUILD_COMMIT_RE = /^[0-9a-f]{7,40}(-dirty)?$/;

/** The deployed commit, or null when the var is absent or malformed (never echoed raw). */
export function buildCommit(env: Env): string | null {
  return typeof env.BUILD_COMMIT === "string" && BUILD_COMMIT_RE.test(env.BUILD_COMMIT) ? env.BUILD_COMMIT : null;
}
