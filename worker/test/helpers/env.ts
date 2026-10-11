// Build a local test Env from the Miniflare bindings plus generated keys.
import { env as baseEnv } from "cloudflare:workers";
import type { Env } from "../../src/env";
import { LOCAL_BINDINGS, TEST_AUD, TEST_ISSUER, type TestKeys } from "./keys";

/** Like Partial<Env>, but an explicit undefined removes a value. */
export type EnvOverrides = { [K in keyof Env]?: Env[K] | undefined };

export function localEnv(keys: TestKeys, overrides: EnvOverrides = {}): Env {
  const base = baseEnv as unknown as Env;
  return {
    ...base,
    ENVIRONMENT: "local",
    LLM_PROVIDER: "stub",
    WAREHOUSE: "duckdb_local",
    PLANNER_MODEL: "stub",
    ACCESS_JWKS_JSON: keys.jwksJson,
    ACCESS_AUD: TEST_AUD,
    ACCESS_ISSUER: TEST_ISSUER,
    ROLE_BINDINGS_JSON: JSON.stringify(LOCAL_BINDINGS),
    WAREHOUSE_URL: "http://127.0.0.1:9/",
    WAREHOUSE_TOKEN: "test-warehouse-token",
    ...overrides,
  } as Env;
}

export const LOCAL_ORIGIN = "http://127.0.0.1:8794";
