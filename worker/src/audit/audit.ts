// Metadata-only audit trail (below the core cut line; optional).
// One event per /api/ask: ids, role, decision codes, catalog key, labels of the
// filter values, counts, bytes and timings. Never the question text, an email,
// a token or a result value. The subject is an HMAC (AUDIT_HMAC_KEY), so the
// audit table cannot be joined back to an identity without the key.
//
// Workers Free plan design (SPEC.md 25.4 A; Basin Pipelines is Workers Paid only):
//   Producer: one of this repo's 400 daily queue slots (AccountBudgetDO), then
//     Queue peoplelens-audit; when no slot is granted or the send fails, the event
//     is put straight into staging at staging/v1/dt=<day>/direct-<request_id>.ndjson,
//     at most free_plan.audit_direct_puts_per_day (2,000) times a day; beyond that it
//     is dropped and logged as audit_dropped (R2 Class A stays inside the free tier).
//   Consumer (queue() in the same Worker): one immutable NDJSON object per batch at
//     staging/v1/dt=<day>/<32 hex of sha256(sorted request ids)>.ndjson, then ackAll;
//     on failure retryAll after 60 s. The key is content-addressed, so a redelivered
//     batch rewrites identical bytes under the same key.
//   Staging: R2 bucket peoplelens-audit (AUDIT_BUCKET) in production; the local
//     sidecar's /v1/audit/stage (.dev/audit-staging) locally.
//   Commit: `pl-ops audit commit` / `pl audit commit` (Python, pyiceberg) appends staged
//     events to the Iceberg table peoplelens.audit_events, deduplicating on request_id.
import type { AskResponse } from "../ask";
import type { Principal } from "../policy/types";

export interface AuditEvent {
  ts: string;
  request_id: string;
  subject_hmac: string;
  role: string;
  action: string;
  metric: string | null;
  group_by: string | null;
  grain: string | null;
  filter_dimensions: string | null;
  filter_values: string | null;
  catalog_key: string | null;
  decision: string | null;
  rows: number | null;
  suppressed_primary: number | null;
  suppressed_complementary: number | null;
  suppressed_hidden_sum: number | null;
  suppressed_complement: number | null;
  estimated_bytes: number | null;
  billed_bytes: number | null;
  latency_ms: number | null;
  llm_ms: number | null;
  warehouse_ms: number | null;
  provider: string | null;
  model: string | null;
  gateway_log_id: string | null;
}

export const AUDIT_FIELDS: readonly (keyof AuditEvent)[] = [
  "ts", "request_id", "subject_hmac", "role", "action", "metric", "group_by", "grain", "filter_dimensions",
  "filter_values", "catalog_key", "decision", "rows", "suppressed_primary", "suppressed_complementary",
  "suppressed_hidden_sum", "suppressed_complement", "estimated_bytes", "billed_bytes", "latency_ms", "llm_ms",
  "warehouse_ms", "provider", "model", "gateway_log_id",
];

export async function subjectHmac(key: string, subject: string): Promise<string> {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(subject));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function buildAuditEvent(res: AskResponse, principal: Principal, hmacKey: string, now: number): Promise<AuditEvent> {
  const a = res.answer;
  const intent = a?.intent;
  const s = a?.suppressed_cells;
  return {
    ts: new Date(now).toISOString(),
    request_id: res.request_id,
    subject_hmac: await subjectHmac(hmacKey, principal.subject),
    role: principal.role,
    action: res.type,
    metric: intent?.metric ?? null,
    group_by: intent ? intent.group_by.join(",") : null,
    grain: intent?.grain ?? null,
    filter_dimensions: intent ? intent.filters.map((f) => f.dimension).join(",") : null,
    filter_values: intent ? intent.filters.map((f) => `${f.dimension}=${f.values.join("|")}`).join(";").slice(0, 200) : null,
    catalog_key: res.meta.catalog_key,
    decision: res.meta.decision,
    rows: a ? a.rows.length : null,
    suppressed_primary: s?.primary ?? null,
    suppressed_complementary: s?.complementary ?? null,
    suppressed_hidden_sum: s?.hidden_sum ?? null,
    suppressed_complement: s?.complement ?? null,
    estimated_bytes: res.meta.estimated_bytes,
    billed_bytes: res.meta.billed_bytes,
    latency_ms: res.meta.latency_ms,
    llm_ms: res.meta.llm_ms,
    warehouse_ms: res.meta.warehouse_ms,
    provider: res.meta.llm_provider,
    model: res.meta.model,
    gateway_log_id: res.meta.gateway_log_id,
  };
}

/** Keep only the whitelisted fields (defence in depth before anything leaves the Worker). */
export function cleanAuditRecord(raw: unknown): AuditEvent | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  for (const f of AUDIT_FIELDS) {
    const v = r[f];
    out[f] = v === undefined ? null : typeof v === "string" ? v.slice(0, 200) : typeof v === "number" ? v : null;
  }
  if (typeof out["ts"] !== "string" || typeof out["request_id"] !== "string") return null;
  return out as unknown as AuditEvent;
}

/** Where staged NDJSON objects go: R2 in production, the local sidecar locally. */
export interface StagingStore {
  readonly kind: "r2" | "local";
  put(key: string, body: string): Promise<void>;
}

export const STAGING_PREFIX = "staging/v1/";
export const BATCH_KEY = /^staging\/v1\/dt=\d{4}-\d{2}-\d{2}\/[0-9a-f]{32}\.ndjson$/;
export const DIRECT_KEY = /^staging\/v1\/dt=\d{4}-\d{2}-\d{2}\/direct-[A-Za-z0-9-]{1,64}\.ndjson$/;
/** Seconds before a failed batch is redelivered. */
export const RETRY_DELAY_S = 60;

const DAY = /^\d{4}-\d{2}-\d{2}$/;

function dayOf(event: AuditEvent, now: number): string {
  const d = event.ts.slice(0, 10);
  return DAY.test(d) ? d : new Date(now).toISOString().slice(0, 10);
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** NDJSON for whitelisted events, sorted by request_id (deterministic bytes). */
export function toNdjson(events: AuditEvent[]): string {
  return [...events]
    .sort((a, b) => (a.request_id < b.request_id ? -1 : a.request_id > b.request_id ? 1 : 0))
    .map((e) => `${JSON.stringify(e)}\n`)
    .join("");
}

/** Content-addressed key for a consumer batch: the same request ids always give the same key. */
export async function batchKey(events: AuditEvent[], now = Date.now()): Promise<string> {
  const ids = events.map((e) => e.request_id).sort();
  const first = [...events].sort((a, b) => (a.request_id < b.request_id ? -1 : 1))[0];
  const day = first ? dayOf(first, now) : new Date(now).toISOString().slice(0, 10);
  return `${STAGING_PREFIX}dt=${day}/${(await sha256Hex(ids.join("\n"))).slice(0, 32)}.ndjson`;
}

/** Key for an event put straight into staging (no queue slot, or the send failed); unique per request. */
export async function directKey(event: AuditEvent, now = Date.now()): Promise<string> {
  const id = /^[A-Za-z0-9-]{1,64}$/.test(event.request_id) ? event.request_id : (await sha256Hex(event.request_id)).slice(0, 32);
  return `${STAGING_PREFIX}dt=${dayOf(event, now)}/direct-${id}.ndjson`;
}

export function r2Store(bucket: R2Bucket): StagingStore {
  return {
    kind: "r2",
    async put(key, body) {
      await bucket.put(key, body, { httpMetadata: { contentType: "application/x-ndjson" } });
    },
  };
}

export function sidecarStore(url: string, token: string, fetcher: typeof fetch = (input, init) => fetch(input, init)): StagingStore {
  return {
    kind: "local",
    async put(key, body) {
      const res = await fetcher(new URL("/v1/audit/stage", url).toString(), {
        method: "POST",
        headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
        body: JSON.stringify({ key, body }),
      });
      if (!res.ok) throw new Error(`local audit staging returned ${res.status}`);
    },
  };
}

/** Stage one consumer batch. Returns the key, or null when nothing in the batch passed the whitelist. */
export async function stageBatch(records: unknown[], store: StagingStore, now = Date.now()): Promise<{ key: string; rows: number } | null> {
  const clean = records.map(cleanAuditRecord).filter((r): r is AuditEvent => r !== null);
  if (clean.length === 0) return null;
  const key = await batchKey(clean, now);
  await store.put(key, toNdjson(clean));
  return { key, rows: clean.length };
}

export interface ProducerDeps {
  queue?: Queue<unknown> | undefined;
  store: StagingStore | null;
  /** One of the day's audit queue slots (AccountBudgetDO); false when the share is used up. */
  reserveSlot(): Promise<boolean>;
  releaseSlot(): Promise<void>;
  /** One of the day's direct staging puts (AccountBudgetDO); false when that share is used up too.
   * Without it, an identity looping on refusals could turn every request into an R2 Class A
   * operation (43,200 a day at the rate limit, above R2's free 1 M a month). */
  reserveDirect?(): Promise<boolean>;
  now?: number;
}

/**
 * Send one event: through the queue when a slot is granted, otherwise (or when the
 * send fails) straight into staging under a key unique to the request, so a Free
 * Queue limit never drops an event; once the day's direct puts are used up as well,
 * the event is dropped ("capped") and the caller logs it.
 */
export async function emitAudit(raw: AuditEvent, deps: ProducerDeps): Promise<"queue" | "direct" | "dropped" | "capped"> {
  const event = cleanAuditRecord(raw);
  if (!event) return "dropped";
  if (deps.queue) {
    let granted = false;
    try {
      granted = await deps.reserveSlot();
    } catch {
      granted = false;
    }
    if (granted) {
      try {
        await deps.queue.send(event);
        return "queue";
      } catch {
        await deps.releaseSlot().catch(() => undefined);
      }
    }
  }
  if (!deps.store) return "dropped";
  if (deps.reserveDirect) {
    let granted = false;
    try {
      granted = await deps.reserveDirect();
    } catch {
      granted = false;
    }
    if (!granted) return "capped";
  }
  await deps.store.put(await directKey(event, deps.now), toNdjson([event]));
  return "direct";
}
