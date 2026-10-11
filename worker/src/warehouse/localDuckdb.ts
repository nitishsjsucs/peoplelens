// Local stand-in for BigQuery: the `pl warehouse serve` sidecar over DuckDB.
// Byte counts are the sidecar's estimate of BigQuery on-demand billing, not a
// BigQuery dry run, and maximumBytesBilled is emulated against that estimate.
import type { BoundQuery } from "../catalog/bind";
import { ApiError } from "../policy/types";
import type { CellValue, RunOptions, RunResult, Warehouse } from "./types";

interface SidecarResponse {
  columns?: string[];
  rows?: CellValue[][];
  estimated_bytes?: number;
  elapsed_ms?: number;
  error?: { code: string; message: string };
}

export class LocalDuckDbWarehouse implements Warehouse {
  readonly kind = "duckdb-local" as const;

  constructor(
    private readonly url: string,
    private readonly token: string,
    private readonly fetcher: typeof fetch = (input, init) => fetch(input, init),
  ) {}

  private async call(q: BoundQuery, dryRun: boolean): Promise<SidecarResponse> {
    if (q.dialect !== "duckdb") throw new ApiError(500, "INTERNAL", "local warehouse needs DuckDB catalog SQL");
    const params: Record<string, string | string[]> = {};
    for (const [name, p] of Object.entries(q.params)) params[name] = p.value;
    const send = () =>
      this.fetcher(new URL("/v1/query", this.url).toString(), {
        method: "POST",
        headers: { authorization: `Bearer ${this.token}`, "content-type": "application/json" },
        body: JSON.stringify({ sql: q.sql, params, dry_run: dryRun }),
      });
    let res: Response;
    try {
      res = await send();
    } catch {
      // A read-only catalog query is idempotent: retry once on a dropped local connection.
      res = await send();
    }
    const json = (await res.json().catch(() => ({}))) as SidecarResponse;
    if (!res.ok) {
      throw new ApiError(502, "INTERNAL", `local warehouse error ${json.error?.code ?? res.status}`);
    }
    return json;
  }

  async dryRun(q: BoundQuery): Promise<{ estimatedBytes: number }> {
    const res = await this.call(q, true);
    return { estimatedBytes: res.estimated_bytes ?? 0 };
  }

  async run(q: BoundQuery, o: RunOptions): Promise<RunResult> {
    const started = Date.now();
    const res = await this.call(q, false);
    if ((res.estimated_bytes ?? 0) > o.maxBytesBilled) {
      throw new ApiError(422, "COST_LIMIT_EXCEEDED", "The query would bill more than your per-query limit (local estimate).");
    }
    return {
      columns: res.columns ?? [],
      rows: res.rows ?? [],
      billedBytes: null,
      elapsedMs: res.elapsed_ms ?? Date.now() - started,
    };
  }
}
