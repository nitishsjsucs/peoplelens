// The hand-written Env must match wrangler.jsonc in both environments.
import { describe, expect, it } from "vitest";
import wranglerText from "../wrangler.jsonc?raw";
import { ENV_VAR_NAMES } from "../src/env";

interface WranglerEnv {
  name?: string;
  assets?: { directory: string; not_found_handling?: string; run_worker_first?: string[] | boolean };
  main?: string;
  workers_dev?: boolean;
  ai?: { binding: string };
  durable_objects?: { bindings: { name: string; class_name: string }[] };
  vars?: Record<string, string>;
}
interface WranglerConfig extends WranglerEnv {
  preview_urls?: boolean;
  observability?: { enabled: boolean };
  migrations?: { tag: string; new_sqlite_classes?: string[] }[];
  env: { production: WranglerEnv };
}

function stripJsonc(text: string): string {
  return text
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n");
}

const config = JSON.parse(stripJsonc(wranglerText)) as WranglerConfig;
const prod = config.env.production;

describe("wrangler.jsonc", () => {
  it("top level is local only: no ai binding, no workers.dev, no preview URLs", () => {
    expect(config.name).toBe("peoplelens-local");
    expect(config.ai).toBeUndefined();
    expect(config.workers_dev).toBe(false);
    expect(config.preview_urls).toBe(false);
    expect(config.observability?.enabled).toBe(true);
    expect(config.main).toBe("src/entry.local.ts");
  });

  it("production is named peoplelens and repeats non-inherited keys", () => {
    expect(prod.name).toBe("peoplelens");
    expect(prod.main).toBe("src/entry.production.ts");
    expect(prod.workers_dev).toBe(true);
    expect(prod.ai).toEqual({ binding: "AI" });
    expect(prod.durable_objects).toEqual(config.durable_objects);
    expect(prod.vars).toMatchObject({ ENVIRONMENT: "production", LLM_PROVIDER: "workers_ai", WAREHOUSE: "bigquery" });
  });

  it("binding and var names match the hand-written Env", () => {
    expect(config.durable_objects?.bindings).toEqual([{ name: "USER_DO", class_name: "UserDO" }]);
    expect(config.migrations).toEqual([{ tag: "v1", new_sqlite_classes: ["UserDO"] }]);
    for (const env of [config, prod]) {
      expect(Object.keys(env.vars ?? {}).sort()).toEqual([...ENV_VAR_NAMES].sort());
    }
  });

  it("serves the UI as static assets with /api/* routed to the Worker first, in both environments", () => {
    for (const env of [config, prod]) {
      expect(env.assets).toEqual({ directory: "../web/dist", not_found_handling: "single-page-application", run_worker_first: ["/api/*"] });
    }
  });

  it("contains no secrets", () => {
    for (const secret of ["GCP_SA_KEY_JSON", "ROLE_BINDINGS_JSON", "ACCESS_AUD", "ACCESS_JWKS_JSON", "WAREHOUSE_TOKEN"]) {
      expect(wranglerText).not.toContain(`"${secret}"`);
    }
  });
});
