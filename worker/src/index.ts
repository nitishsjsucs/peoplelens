// Hono app: routes, auth middleware and the request pipeline.
import { Hono } from "hono";
import { ACCESS_HEADER, verifyAccessJwt } from "./auth/access";
import { resolvePrincipal } from "./auth/principal";
import type { CatalogFile } from "./catalog/catalog";
import { type Config, type Env, loadConfig } from "./env";
import { logEvent } from "./log";
import { POLICY, roleDimensions } from "./policy/policy";
import { ApiError, type Principal } from "./policy/types";

export interface AppDeps {
  catalog: CatalogFile;
}

type Vars = { config: Config; principal: Principal; requestId: string };

function errorBody(err: ApiError, requestId: string) {
  return {
    request_id: requestId,
    type: "refusal" as const,
    refusal: { code: err.code, message: err.message, alternatives: err.alternatives },
  };
}

export function createApp(deps: AppDeps) {
  const app = new Hono<{ Bindings: Env; Variables: Vars }>();

  app.onError((err, c) => {
    const requestId = c.get("requestId") ?? crypto.randomUUID();
    if (err instanceof ApiError) {
      logEvent({ event: "refusal", request_id: requestId, decision: err.code, status: err.status });
      return c.json(errorBody(err, requestId), err.status as 400);
    }
    logEvent({ event: "error", request_id: requestId, decision: "INTERNAL", status: 500 });
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
    if (!result.ok) {
      throw new ApiError(503, "CONFIG_INVALID", "service is not configured; see /api/health");
    }
    const claims = await verifyAccessJwt(c.req.header(ACCESS_HEADER), result.config.access);
    const principal = resolvePrincipal(claims, result.config.roleBindings);
    c.set("config", result.config);
    c.set("principal", principal);
    await next();
  });

  app.get("/api/me", (c) => {
    const p = c.get("principal");
    const role = POLICY.roles[p.role];
    return c.json({
      principal: { subject: p.subject, kind: p.kind },
      role: p.role,
      scope: p.scope,
      metrics: role.metrics,
      dimensions: role.dimensions,
      grains: role.grains,
    });
  });

  app.get("/api/catalog", (c) => {
    const p = c.get("principal");
    const role = POLICY.roles[p.role];
    return c.json({
      metrics: role.metrics.map((id) => ({
        id,
        label: POLICY.metrics[id].label,
        definition: POLICY.metrics[id].definition,
        unit: POLICY.metrics[id].unit,
        dimensions: roleDimensions(POLICY, p.role, id),
      })),
      grains: role.grains,
    });
  });

  app.notFound((c) => c.json({ error: { code: "NOT_FOUND" } }, 404));
  return app;
}

export function createWorker(catalog: CatalogFile): ExportedHandler<Env> {
  const app = createApp({ catalog });
  return {
    fetch: (request, env, ctx) => app.fetch(request, env, ctx),
  };
}
