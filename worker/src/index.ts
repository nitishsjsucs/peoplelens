// Hono app: routes, auth middleware and wiring of the request pipeline.
import { Hono } from "hono";
import { type AskBody, type UserPort, ask } from "./ask";
import { RETRY_DELAY_S, type StagingStore, buildAuditEvent, emitAudit, r2Store, sidecarStore, stageBatch } from "./audit/audit";
import { ACCESS_HEADER, verifyAccessJwt } from "./auth/access";
import { resolvePrincipal } from "./auth/principal";
import type { CatalogFile } from "./catalog/catalog";
import { type Config, type Env, buildCommit, loadConfig } from "./env";
import { OpenAiCompatClient } from "./llm/openaiCompat";
import { StubPlanner } from "./llm/stub";
import type { LlmClient } from "./llm/types";
import { WorkersAiClient } from "./llm/workersAi";
import { ACCOUNT_BUDGET_NAME, type BudgetCounter, utcDay } from "./session/budget";
import { logEvent } from "./log";
import { POLICY } from "./policy/policy";
import { ApiError, type Principal } from "./policy/types";
import { listMetrics } from "./tools/registry";
import { BigQueryWarehouse } from "./warehouse/bigquery";
import { GoogleTokenSource, parseServiceAccountKey } from "./warehouse/googleAuth";
import { LocalDuckDbWarehouse } from "./warehouse/localDuckdb";
import type { Warehouse } from "./warehouse/types";
import { warmUp } from "./warmup";

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

export const DEFAULT_PLANNER_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";

function accountBudget(env: Env) {
  return env.ACCOUNT_BUDGET.get(env.ACCOUNT_BUDGET.idFromName(ACCOUNT_BUDGET_NAME));
}

/** One AccountBudgetDO counter, bound to the UTC day of `now` (a request that crosses
 * midnight settles against the day it reserved on). */
export function budgetPort(env: Env, counter: BudgetCounter, cap: number, now: number) {
  const day = utcDay(now);
  const stub = accountBudget(env);
  return {
    day,
    reserve: (amount: number) => stub.reserve(day, counter, amount, cap),
    settle: (reserved: number, actual: number | null) => stub.settle(day, counter, reserved, actual),
  };
}

export function defaultLlm(config: Config, env: Env, now: () => number = () => Date.now()): LlmClient {
  switch (config.llmProvider) {
    case "workers_ai": {
      if (!env.AI || !config.aiGatewayId) throw new ApiError(503, "CONFIG_INVALID", "Workers AI is not configured");
      if (!env.ACCOUNT_BUDGET) throw new ApiError(503, "CONFIG_INVALID", "ACCOUNT_BUDGET is not bound");
      const model = config.plannerModel || DEFAULT_PLANNER_MODEL;
      const free = POLICY.limits.free_plan;
      return new WorkersAiClient(env.AI, model, config.aiGatewayId, now, {
        port: budgetPort(env, "neurons", free.worker_neurons_per_day, now()),
        reserveNeurons: free.planner_reserve_neurons,
        rates: free.model_neurons_per_m_tokens[model] ?? null,
      });
    }
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
  // One pure pass over the request path at startup, so a cold isolate's first question
  // does not pay for compiling it (Workers Free: 10 ms of CPU per request; src/warmup.ts).
  warmUp(deps.catalog);
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
        { ok: false, environment: c.env.ENVIRONMENT ?? "local", missing: result.missing, problems: result.problems, build_commit: buildCommit(c.env) },
        503,
      );
    }
    return c.json({
      ok: true,
      environment: result.config.environment,
      warehouse: result.config.warehouse,
      llm_provider: result.config.llmProvider,
      planner_model: result.config.plannerModel || null,
      catalog_entries: deps.catalog.header.entries,
      build_commit: buildCommit(c.env),
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

  app.get("/api/me", async (c) => {
    const p = c.get("principal");
    // This repo's Workers AI neuron share today (the eval runner and smoke read it).
    const day = utcDay(now());
    const cap = POLICY.limits.free_plan.worker_neurons_per_day;
    const used = c.env.ACCOUNT_BUDGET ? (await accountBudget(c.env).usage(day)).neurons : null;
    return c.json({
      principal: { subject: p.subject, kind: p.kind },
      role: p.role,
      scope: p.scope,
      ...describeRole(p),
      ai_budget: { day, used, cap },
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
        llm: deps.makeLlm ? deps.makeLlm(config, c.env) : defaultLlm(config, c.env, now),
        user: userPort(c.env, principal),
        principal,
        requestId: c.get("requestId"),
        now,
      },
      body,
    );
    const store = stagingStore(c.env);
    if (c.env.AUDIT_HMAC_KEY && (c.env.AUDIT_QUEUE || store)) {
      const t = now();
      const free = POLICY.limits.free_plan;
      const slots = budgetPort(c.env, "audit_queue_messages", free.audit_queue_messages_per_day, t);
      const direct = budgetPort(c.env, "audit_direct_puts", free.audit_direct_puts_per_day, t);
      const send = buildAuditEvent(result.body, principal, c.env.AUDIT_HMAC_KEY, t)
        .then((event) =>
          emitAudit(event, {
            queue: c.env.AUDIT_QUEUE,
            store,
            reserveSlot: async () => (await slots.reserve(1)).ok,
            releaseSlot: () => slots.settle(1, 0),
            reserveDirect: async () => (await direct.reserve(1)).ok,
            now: t,
          }),
        )
        .then((where) => {
          if (where === "capped") logEvent({ event: "audit_dropped", request_id: result.body.request_id, decision: where });
          else if (where !== "queue") logEvent({ event: "audit_event", request_id: result.body.request_id, decision: where });
        })
        .catch(() => logEvent({ event: "audit_error", request_id: result.body.request_id }));
      let waited = false;
      try {
        c.executionCtx.waitUntil(send);
        waited = true;
      } catch {
        // no execution context (unit tests): fall through and await
      }
      if (!waited) await send;
    }
    return c.json(result.body, result.status as 200, result.headers ?? {});
  });

  app.notFound((c) => c.json({ error: { code: "NOT_FOUND" } }, 404));
  return app;
}

function describeRole(p: Principal) {
  const role = POLICY.roles[p.role];
  return { metrics: role.metrics, dimensions: role.dimensions, grains: role.grains };
}

/** The audit staging store: the R2 bucket in production, the local sidecar otherwise. */
export function stagingStore(env: Env, fetcher?: typeof fetch): StagingStore | null {
  if (env.AUDIT_BUCKET) return r2Store(env.AUDIT_BUCKET);
  if (env.ENVIRONMENT !== "production" && env.WAREHOUSE_URL && env.WAREHOUSE_TOKEN) {
    return sidecarStore(env.WAREHOUSE_URL, env.WAREHOUSE_TOKEN, fetcher);
  }
  return null;
}

/**
 * Queue consumer for the audit trail: writes the batch as one immutable,
 * content-addressed NDJSON object to staging, then acks; on any failure (including
 * a missing store) every message is retried after 60 s, up to the queue's max_retries.
 */
export async function handleAuditBatch(batch: MessageBatch<unknown>, env: Env, fetcher?: typeof fetch): Promise<void> {
  const store = stagingStore(env, fetcher);
  try {
    if (!store) throw new Error("no audit staging store is bound");
    const staged = await stageBatch(batch.messages.map((m) => m.body), store);
    logEvent({ event: "audit_batch", decision: staged ? `staged_${store.kind}` : "dropped_invalid", rows: staged?.rows ?? 0 });
    batch.ackAll();
  } catch {
    logEvent({ event: "audit_batch", decision: "retry", rows: batch.messages.length });
    batch.retryAll({ delaySeconds: RETRY_DELAY_S });
  }
}

export function createWorker(catalog: CatalogFile): ExportedHandler<Env> {
  const app = createApp({ catalog });
  return {
    fetch: (request, env, ctx) => app.fetch(request, env, ctx),
    queue: (batch, env) => handleAuditBatch(batch, env),
  };
}
