import { afterEach, describe, expect, it, vi } from "vitest";
import bqJson from "../src/generated/catalog.bigquery.json";
import type { CatalogFile } from "../src/catalog/catalog";
import { warmUpPasses } from "../src/warmup";
import { DUCK_CATALOG } from "./helpers/app";

afterEach(() => vi.restoreAllMocks());

describe("startup warm-up", () => {
  it("runs both questions through to the narrative on either catalog, silently and without I/O", () => {
    const logs = vi.spyOn(console, "log");
    const errors = vi.spyOn(console, "error");
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    expect(warmUpPasses(DUCK_CATALOG)).toBe(2);
    expect(warmUpPasses(bqJson as unknown as CatalogFile)).toBe(2);
    expect(logs).not.toHaveBeenCalled();
    expect(errors).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
