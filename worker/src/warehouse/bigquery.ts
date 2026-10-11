// BigQuery client over REST (jobs.query + getQueryResults), production only.
// Every job carries maximumBytesBilled equal to the role cap, typed named
// parameters, labels, and a timeout; dry runs read totalBytesProcessed and are
// cached per catalog key (bytes do not depend on parameter values for these
// unpartitioned tables).
//
// Workers Free allows 50 external subrequests per invocation (SPEC.md 25.4 D). The
// dry-run cache is module-level (a Worker builds a new client per request) and holds
// promises, so a main query and its complement probes share one dry run per catalog
// key per isolate-hour; polling is capped at free_plan.bq_max_polls_per_query per
// query. With the token and JWKS caches that bounds a question at 27 external fetches
// (free_plan.max_external_subrequests: the main query and up to 5 complement probes).
import type { BoundQuery, ParamValue } from "../catalog/bind";
import { POLICY } from "../policy/policy";
import { ApiError } from "../policy/types";
import type { GoogleTokenSource } from "./googleAuth";
import type { CellValue, RunOptions, RunResult, Warehouse } from "./types";

export const BQ_API = "https://bigquery.googleapis.com/bigquery/v2";
export const BYTES_BILLED_MESSAGE = "Query exceeded limit for bytes billed";

export interface BigQueryParameter {
  name: string;
  parameterType: { type: string; arrayType?: { type: string } };
  parameterValue: { value?: string; arrayValues?: { value: string }[] };
}

export function toQueryParameters(params: Record<string, ParamValue>): BigQueryParameter[] {
  return Object.entries(params).map(([name, p]) => {
    if (p.type === "ARRAY<STRING>") {
      return {
        name,
        parameterType: { type: "ARRAY", arrayType: { type: "STRING" } },
        parameterValue: { arrayValues: p.value.map((value) => ({ value })) },
      };
    }
    return { name, parameterType: { type: p.type }, parameterValue: { value: p.value } };
  });
}

interface BqField {
  name: string;
  type: string;
}
interface BqQueryResponse {
  jobComplete?: boolean;
  jobReference?: { projectId: string; jobId: string; location?: string };
  schema?: { fields: BqField[] };
  rows?: { f: { v: unknown }[] }[];
  totalBytesProcessed?: string;
  totalBytesBilled?: string;
  pageToken?: string;
  error?: { code: number; message: string; errors?: { reason?: string; message?: string }[] };
}

/** BigQuery REST returns TIMESTAMP as epoch seconds ("1.7040672E9") or, with
 * useInt64Timestamp, as epoch microseconds; dates come back as "YYYY-MM-DD". */
export function decodeValue(type: string, v: unknown): CellValue {
  if (v === null || v === undefined) return null;
  const s = String(v);
  switch (type) {
    case "INTEGER":
    case "INT64":
    case "FLOAT":
    case "FLOAT64":
    case "NUMERIC":
    case "BIGNUMERIC":
      return Number(s);
    case "TIMESTAMP": {
      const ms = /^-?\d+$/.test(s) && s.length > 12 ? Number(s) / 1000 : Number(s) * 1000;
      return new Date(ms).toISOString().slice(0, 10);
    }
    default:
      return s;
  }
}

function costError(message: string | undefined): boolean {
  return typeof message === "string" && message.includes(BYTES_BILLED_MESSAGE);
}

export interface DryRunEntry {
  at: number;
  bytes: Promise<number>;
}

/** One dry run per project and catalog key per isolate (entries expire after the TTL). */
const sharedDryRuns = new Map<string, DryRunEntry>();

export function clearDryRunCache(): void {
  sharedDryRuns.clear();
}

export const MAX_POLLS_PER_QUERY = POLICY.limits.free_plan.bq_max_polls_per_query;

export class BigQueryWarehouse implements Warehouse {
  readonly kind = "bigquery" as const;

  constructor(
    private readonly opts: {
      projectId: string;
      tokens: GoogleTokenSource;
      fetcher?: typeof fetch;
      location?: string;
      dryRunCacheMs?: number;
      now?: () => number;
      /** Defaults to the isolate-wide cache; tests pass their own. */
      dryRunCache?: Map<string, DryRunEntry>;
      maxPolls?: number;
    },
  ) {}

  private get fetcher(): typeof fetch {
    return this.opts.fetcher ?? ((input, init) => fetch(input, init));
  }

  private now(): number {
    return this.opts.now ? this.opts.now() : Date.now();
  }

  private body(q: BoundQuery, extra: Record<string, unknown>): Record<string, unknown> {
    return {
      query: q.sql,
      useLegacySql: false,
      parameterMode: "NAMED",
      queryParameters: toQueryParameters(q.params),
      location: this.opts.location ?? "US",
      formatOptions: { useInt64Timestamp: true },
      ...extra,
    };
  }

  private async post(path: string, body: Record<string, unknown>): Promise<BqQueryResponse> {
    const token = await this.opts.tokens.token();
    const res = await this.fetcher(`${BQ_API}/projects/${this.opts.projectId}${path}`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const json = (await res.json().catch(() => ({}))) as BqQueryResponse;
    if (!res.ok || json.error) {
      const message = json.error?.message ?? `BigQuery HTTP ${res.status}`;
      if (costError(message) || json.error?.errors?.some((e) => costError(e.message))) {
        throw new ApiError(422, "COST_LIMIT_EXCEEDED", "BigQuery refused the query: it would bill more than your per-query limit.");
      }
      throw new ApiError(502, "INTERNAL", "BigQuery request failed");
    }
    return json;
  }

  async dryRun(q: BoundQuery): Promise<{ estimatedBytes: number }> {
    const ttl = this.opts.dryRunCacheMs ?? POLICY.limits.dry_run_cache_seconds * 1000;
    const cache = this.opts.dryRunCache ?? sharedDryRuns;
    const key = `${this.opts.projectId}|${q.catalogKey}`;
    let entry = cache.get(key);
    if (!entry || this.now() - entry.at >= ttl) {
      // The promise is cached before anything is awaited, so parallel calls for the
      // main query and its probes share this one request.
      const fresh: DryRunEntry = {
        at: this.now(),
        bytes: this.post("/queries", this.body(q, { dryRun: true })).then((res) => Number(res.totalBytesProcessed ?? "0")),
      };
      cache.set(key, fresh);
      fresh.bytes.catch(() => {
        if (cache.get(key) === fresh) cache.delete(key);
      });
      entry = fresh;
    }
    return { estimatedBytes: await entry.bytes };
  }

  async run(q: BoundQuery, o: RunOptions): Promise<RunResult> {
    const started = this.now();
    let res = await this.post(
      "/queries",
      this.body(q, {
        maximumBytesBilled: String(o.maxBytesBilled),
        timeoutMs: o.timeoutMs,
        jobTimeoutMs: "15000",
        useQueryCache: true,
        labels: o.labels,
      }),
    );
    const maxPolls = this.opts.maxPolls ?? MAX_POLLS_PER_QUERY;
    let polls = 0;
    while (!res.jobComplete) {
      const job = res.jobReference;
      if (!job || polls >= maxPolls || this.now() - started > o.timeoutMs) {
        throw new ApiError(504, "INTERNAL", "BigQuery query timed out");
      }
      polls += 1;
      const token = await this.opts.tokens.token();
      const params = new URLSearchParams({
        location: job.location ?? this.opts.location ?? "US",
        timeoutMs: String(Math.max(1000, o.timeoutMs - (this.now() - started))),
        "formatOptions.useInt64Timestamp": "true",
      });
      const poll = await this.fetcher(`${BQ_API}/projects/${job.projectId}/queries/${job.jobId}?${params}`, {
        headers: { authorization: `Bearer ${token}` },
      });
      const json = (await poll.json().catch(() => ({}))) as BqQueryResponse;
      if (!poll.ok || json.error) {
        if (costError(json.error?.message)) {
          throw new ApiError(422, "COST_LIMIT_EXCEEDED", "BigQuery refused the query: it would bill more than your per-query limit.");
        }
        throw new ApiError(502, "INTERNAL", "BigQuery request failed");
      }
      const billed = json.totalBytesBilled ?? res.totalBytesBilled;
      res = billed !== undefined ? { ...json, totalBytesBilled: billed } : json;
    }
    const fields = res.schema?.fields ?? [];
    const rows = (res.rows ?? []).map((r) => r.f.map((cell, i) => decodeValue(fields[i]?.type ?? "STRING", cell.v)));
    return {
      columns: fields.map((f) => f.name),
      rows,
      billedBytes: res.totalBytesBilled !== undefined ? Number(res.totalBytesBilled) : null,
      elapsedMs: this.now() - started,
    };
  }
}
