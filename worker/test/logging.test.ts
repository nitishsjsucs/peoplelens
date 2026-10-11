import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { LOG_KEYS } from "../src/log";
import { harness } from "./helpers/app";
import { ROLE_EMAILS, type TestKeys, makeKeys } from "./helpers/keys";

let keys: TestKeys;
beforeAll(async () => {
  keys = await makeKeys();
});
afterEach(() => vi.restoreAllMocks());

describe("whitelisted logging", () => {
  it("never writes question text or result values to the console", async () => {
    const lines: string[] = [];
    for (const level of ["log", "info", "warn", "error", "debug"] as const) {
      vi.spyOn(console, level).mockImplementation((...args: unknown[]) => {
        lines.push(args.map((a) => (typeof a === "string" ? a : JSON.stringify(a))).join(" "));
      });
    }
    const h = harness(keys);
    const A = ROLE_EMAILS.people_analytics;
    const s = await h.session(A);
    const marker = "ZEBRA-MARKER-7731";
    const r = (await h.ask(A, s, `${marker} headcount by department`)) as Record<string, any>;
    expect(r.type).toBe("answer");
    await h.ask(A, s, `${marker} what about by location`);
    await h.ask(A, s, `${marker} headcount by gender`);
    const values = new Set<number>();
    for (const row of r.answer.rows as Record<string, unknown>[]) {
      if (typeof row["headcount"] === "number") values.add(row["headcount"] as number);
    }
    expect(lines.length).toBeGreaterThan(0);
    for (const line of lines) {
      expect(line).not.toContain(marker);
      expect(line).not.toContain(A);
      const parsed = JSON.parse(line) as Record<string, unknown>;
      for (const [k, v] of Object.entries(parsed)) {
        expect(LOG_KEYS.has(k), k).toBe(true);
        if (typeof v === "number" && v >= 100) expect(values.has(v), `${k}=${v}`).toBe(false);
      }
    }
  });
});
