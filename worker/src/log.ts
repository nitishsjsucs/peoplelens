// Whitelisted structured logging. Only these keys are ever written; question
// text, result values, emails and tokens can never reach Workers Logs.
const ALLOWED_KEYS = new Set([
  "event",
  "request_id",
  "route",
  "status",
  "role",
  "action",
  "catalog_key",
  "decision",
  "latency_ms",
  "llm_ms",
  "warehouse_ms",
  "estimated_bytes",
  "billed_bytes",
  "suppressed",
  "provider",
  "model",
  "gateway_log_id",
  "probes",
  "rows",
  "error_name",
]);

type Scalar = string | number | boolean | null;

function clean(value: unknown): Scalar | Record<string, number> | undefined {
  if (value === null || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "string") return value.length > 120 ? value.slice(0, 120) : value;
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const out: Record<string, number> = {};
    for (const [k, v] of Object.entries(value)) if (typeof v === "number") out[k] = v;
    return out;
  }
  return undefined;
}

export function logEvent(fields: Record<string, unknown>): void {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(fields)) {
    if (!ALLOWED_KEYS.has(key)) continue;
    const v = clean(value);
    if (v !== undefined) out[key] = v;
  }
  console.log(JSON.stringify(out));
}

export const LOG_KEYS: ReadonlySet<string> = ALLOWED_KEYS;
