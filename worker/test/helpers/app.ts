// Build the Hono app with fakes and send authenticated requests to it.
import duckJson from "../../src/generated/catalog.duckdb.json";
import type { CatalogFile } from "../../src/catalog/catalog";
import type { Env } from "../../src/env";
import { createApp } from "../../src/index";
import { StubPlanner } from "../../src/llm/stub";
import type { LlmClient, PlanCall, PlannerInput } from "../../src/llm/types";
import type { Warehouse } from "../../src/warehouse/types";
import { localEnv } from "./env";
import { FakeWarehouse } from "./fakeWarehouse";
import { type TestKeys, mintToken, userClaims } from "./keys";

export const DUCK_CATALOG = duckJson as unknown as CatalogFile;
export const ORIGIN = "http://127.0.0.1:8794";

export class RecordingLlm implements LlmClient {
  readonly provider = "stub" as const;
  readonly model = "recording";
  inputs: PlannerInput[] = [];
  constructor(private readonly inner: LlmClient = new StubPlanner(), private readonly scripted: unknown[] = []) {}
  async plan(input: PlannerInput): Promise<PlanCall> {
    this.inputs.push(input);
    const next = this.scripted.shift();
    if (next !== undefined) return { raw: next, ms: 1 };
    return this.inner.plan(input);
  }
}

export interface Harness {
  env: Env;
  warehouse: FakeWarehouse;
  llm: RecordingLlm;
  clock: { t: number };
  call(email: string, path: string, body?: unknown): Promise<{ status: number; json: Record<string, unknown>; headers: Headers }>;
  session(email: string): Promise<string>;
  ask(email: string, session: string, message: string): Promise<Record<string, unknown>>;
  choose(email: string, session: string, option: string): Promise<Record<string, unknown>>;
}

export function harness(
  keys: TestKeys,
  opts: { warehouse?: FakeWarehouse; llm?: RecordingLlm; tick?: number; makeLlm?: (env: Env, now: number) => LlmClient } = {},
): Harness {
  const warehouse = opts.warehouse ?? new FakeWarehouse();
  const llm = opts.llm ?? new RecordingLlm();
  const clock = { t: Date.UTC(2026, 9, 10, 12) };
  const app = createApp({
    catalog: DUCK_CATALOG,
    makeWarehouse: () => warehouse as Warehouse,
    makeLlm: (_config, env) => (opts.makeLlm ? opts.makeLlm(env, clock.t) : llm),
    now: () => clock.t,
  });
  const env = localEnv(keys);
  const h: Harness = {
    env,
    warehouse,
    llm,
    clock,
    async call(email, path, body) {
      clock.t += opts.tick ?? 2500;
      const token = await mintToken(keys, { claims: userClaims(email) });
      const init: RequestInit = { method: body === undefined ? "GET" : "POST", headers: { "Cf-Access-Jwt-Assertion": token } };
      if (body !== undefined) {
        init.body = JSON.stringify(body);
        (init.headers as Record<string, string>)["content-type"] = "application/json";
      }
      const res = await app.fetch(new Request(`${ORIGIN}${path}`, init), env);
      return { status: res.status, json: (await res.json()) as Record<string, unknown>, headers: res.headers };
    },
    async session(email) {
      const r = await h.call(email, "/api/sessions", {});
      return r.json["session_id"] as string;
    },
    async ask(email, session, message) {
      return (await h.call(email, "/api/ask", { session_id: session, message })).json;
    },
    async choose(email, session, option) {
      return (await h.call(email, "/api/ask", { session_id: session, choose_option: option })).json;
    },
  };
  return h;
}
