// Metadata-only audit trail (below the core cut line; optional).
// One event per /api/ask: ids, role, decision codes, catalog key, labels of the
// filter values, counts, bytes and timings. Never the question text, an email,
// a token or a result value. The subject is an HMAC (AUDIT_HMAC_KEY), so the
// audit table cannot be joined back to an identity without the key.
//   Production: Queue peoplelens-audit -> queue() -> Basin Pipelines stream AUDIT_STREAM -> Iceberg.
//   Local: Miniflare queue -> queue() -> sidecar /v1/audit -> pyiceberg SqlCatalog.
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

export interface AuditSinks {
  stream?: { send(records: unknown[]): Promise<void> };
  localUrl?: string;
  localToken?: string;
  fetcher?: typeof fetch;
}

/** Forward a batch: Basin Pipelines stream in production, the local sidecar otherwise. */
export async function forwardAuditBatch(records: unknown[], sinks: AuditSinks): Promise<"stream" | "local" | "dropped"> {
  const clean = records.map(cleanAuditRecord).filter((r): r is AuditEvent => r !== null);
  if (clean.length === 0) return "dropped";
  if (sinks.stream) {
    await sinks.stream.send(clean);
    return "stream";
  }
  if (sinks.localUrl && sinks.localToken) {
    const fetcher = sinks.fetcher ?? ((input, init) => fetch(input, init));
    const res = await fetcher(new URL("/v1/audit", sinks.localUrl).toString(), {
      method: "POST",
      headers: { authorization: `Bearer ${sinks.localToken}`, "content-type": "application/json" },
      body: JSON.stringify({ records: clean }),
    });
    if (!res.ok) throw new Error(`local audit sink returned ${res.status}`);
    return "local";
  }
  return "dropped";
}
