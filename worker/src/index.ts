// Hono app: routes, auth middleware and wiring of the request pipeline.
import { Hono } from "hono";
import { type AskBody, type UserPort, ask } from "./ask";
import { ACCESS_HEADER, verifyAccessJwt } from "./auth/access";
import { resolvePrincipal } from "./auth/principal";
import type { CatalogFile } from "./catalog/catalog";
import { type Config, type Env, loadConfig } from "./env";
import { OpenAiCompatClient } from "./llm/openaiCompat";
import { StubPlanner } from "./llm/stub";
import type { LlmClient } from "./llm/types";
import { WorkersAiClient } from "./llm/workersAi";
import { logEvent } from "./log";
import { POLICY } from "./policy/policy";
import { ApiError, type Principal } from "./policy/types";
import { listMetrics } from "./tools/registry";
import { BigQueryWarehouse } from "./warehouse/bigquery";
import { GoogleTokenSource, parseServiceAccountKey } from "./warehouse/googleAuth";
import { LocalDuckDbWarehouse } from "./warehouse/localDuckdb";
import type { Warehouse } from "./warehouse/types";

export interface AppDeps {
  catalog: CatalogFile;
  makeWarehouse?: (config: Config, env: Env) => Warehouse;
  makeLlm?: (config: Config, env: Env) => LlmClient;
  now?: () => number;
}

type Vars = { config: Config; principal: Principal; requestId: string };

export function defaultWarehouse(config: Config): Warehouse {
  if (config.warehouse === "bigquery") {
    if (!config.gcpProjectId) throw new ApiError(503, "CONFIG_INVALID", "GCP_PROJECT_ID is missing");
    const tokens = new GoogleTokenSource(parseServiceAccountKey(config.gcpSaKeyJson));
    return new BigQueryWarehouse({ projectId: config.gcpProjectId, tokens });
  }
  if (!config.warehouseUrl || !config.warehouseToken) throw new ApiError(503, "CONFIG_INVALID", "local warehouse is not configured");
  return new LocalDuckDbWarehouse(config.warehouseUrl, config.warehouseToken);
}

export function defaultLlm(config: Config, env: Env): LlmClient {
  switch (config.llmProvider) {
    case "workers_ai":
      if (!env.AI || !config.aiGatewayId) throw new ApiError(503, "CONFIG_INVALID", "Workers AI is not configured");
      return new WorkersAiClient(env.AI, config.plannerModel || "@cf/meta/llama-3.3-70b-instruct-fp8-fast", config.aiGatewayId);
    case "openai_compat":
      if (!config.llmBaseUrl) throw new ApiError(503, "CONFIG_INVALID", "LLM_BASE_URL is missing");
      return new OpenAiCompatClient(config.llmBaseUrl, config.plannerModel || "qwen3-1.7b");
    case "stub":
      return new StubPlanner();
  }
}

function userPort(env: Env, principal: Principal): UserPort {
  return env.USER_DO.get(env.USER_DO.idFromName(principal.subject)) as unknown as UserPort;
}

export function createApp(deps: AppDeps) {
  const app = new Hono<{ Bindings: Env; Variables: Vars }>();
  const now = deps.now ?? (() => Date.now());

  app.onError((err, c) => {
    const requestId = c.get("requestId") ?? crypto.randomUUID();
    if (err instanceof ApiError) {
      logEvent({ event: "refusal", request_id: requestId, decision: err.code, status: err.status });
      return c.json(
        { request_id: requestId, type: "refusal", refusal: { code: err.code, message: err.message, alternatives: err.alternatives } },
        err.status as 400,
      );
    }
    logEvent({ event: "error", request_id: requestId, decision: "INTERNAL", status: 500, error_name: err instanceof Error ? err.name : "unknown" });
    return c.json({ request_id: requestId, type: "error", error: { code: "INTERNAL" } }, 500);
  });

  app.use("/api/*", async (c, next) => {
    c.set("requestId", crypto.randomUUID());
    await next();
  });

  app.get("/api/health", (c) => {
    const result = loadConfig(c.env, new URL(c.req.url));
    if (!result.ok) {
      return c.json(
        { ok: false, environment: c.env.ENVIRONMENT ?? "local", missing: result.missing, problems: result.problems },
        503,
      );
    }
    return c.json({
      ok: true,
      environment: result.config.environment,
      warehouse: result.config.warehouse,
      llm_provider: result.config.llmProvider,
      catalog_entries: deps.catalog.header.entries,
    });
  });

  // Everything else under /api requires a valid configuration and a verified Access JWT.
  app.use("/api/*", async (c, next) => {
    const result = loadConfig(c.env, new URL(c.req.url));
    if (!result.ok) throw new ApiError(503, "CONFIG_INVALID", "service is not configured; see /api/health");
    const expected = result.config.warehouse === "bigquery" ? "bigquery" : "duckdb";
    if (deps.catalog.header.dialect !== expected) throw new ApiError(503, "CONFIG_INVALID", "catalog dialect does not match the warehouse");
    const claims = await verifyAccessJwt(c.req.header(ACCESS_HEADER), result.config.access);
    c.set("config", result.config);
    c.set("principal", resolvePrincipal(claims, result.config.roleBindings));
    await next();
  });

  app.get("/api/me", (c) => {
    const p = c.get("principal");
    return c.json({
      principal: { subject: p.subject, kind: p.kind },
      role: p.role,
      scope: p.scope,
      ...describeRole(p),
    });
  });

  app.get("/api/catalog", async (c) => c.json(await listMetrics.run({}, { principal: c.get("principal") })));

  app.post("/api/sessions", async (c) => {
    const id = await userPort(c.env, c.get("principal")).createSession(now());
    return c.json({ session_id: id });
  });

  app.post("/api/ask", async (c) => {
    let body: AskBody;
    try {
      body = (await c.req.json()) as AskBody;
    } catch {
      throw new ApiError(400, "BAD_REQUEST", "body must be JSON");
    }
    if (!body || typeof body !== "object") throw new ApiError(400, "BAD_REQUEST", "body must be an object");
    const config = c.get("config");
    const principal = c.get("principal");
    const result = await ask(
      {
        catalog: deps.catalog,
        dialect: deps.catalog.header.dialect,
        ...(config.gcpProjectId ? { projectId: config.gcpProjectId } : {}),
        warehouse: (deps.makeWarehouse ?? defaultWarehouse)(config, c.env),
        llm: (deps.makeLlm ?? defaultLlm)(config, c.env),
        user: userPort(c.env, principal),
        principal,
        requestId: c.get("requestId"),
        now,
      },
      body,
    );
    return c.json(result.body, result.status as 200);
  });

  app.notFound((c) => c.json({ error: { code: "NOT_FOUND" } }, 404));
  return app;
}

function describeRole(p: Principal) {
  const role = POLICY.roles[p.role];
  return { metrics: role.metrics, dimensions: role.dimensions, grains: role.grains };
}

export function createWorker(catalog: CatalogFile): ExportedHandler<Env> {
  const app = createApp({ catalog });
  return {
    fetch: (request, env, ctx) => app.fetch(request, env, ctx),
  };
}
