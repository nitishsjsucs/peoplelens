// Deterministic narrative templates. Built only from rows that already passed
// suppression, so a hidden value can never appear in the text.
import type { OutputRow, SuppressionCounts } from "../guard/suppression";
import type { DimensionId, MetricDef, Policy, QueryIntent } from "../policy/types";

const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

// Number formatters are built once per isolate at startup. The first locale-aware
// format in an isolate initializes ICU number formatting, about 20 ms of CPU in
// `npm run bench:cpu` (SPEC.md 23), which would land on the first answer of a cold
// isolate against the Workers Free 10 ms limit; formatting once here moves it to startup.
const INTEGER = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const ONE_DECIMAL = new Intl.NumberFormat("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
INTEGER.format(1780);
ONE_DECIMAL.format(1724);

export function formatValue(unit: string, v: number | null): string {
  if (v === null) return "hidden";
  switch (unit) {
    case "employees":
      return INTEGER.format(Math.round(v));
    case "FTE":
      return ONE_DECIMAL.format(v);
    case "ratio":
      return `${(v * 100).toFixed(1)}%`;
    case "days":
      return `${v.toFixed(1)} days`;
    case "USD":
      return `$${INTEGER.format(Math.round(v))}`;
    default:
      return String(v);
  }
}

export function periodLabel(period: string, grain: QueryIntent["grain"]): string {
  const y = period.slice(0, 4);
  const m = Number(period.slice(5, 7));
  if (grain === "year") return y;
  if (grain === "quarter") return `Q${Math.floor((m - 1) / 3) + 1} ${y}`;
  return `${MONTH_NAMES[m - 1]?.slice(0, 3) ?? ""} ${y}`;
}

function longDate(iso: string): string {
  const m = Number(iso.slice(5, 7));
  return `${MONTH_NAMES[m - 1] ?? ""} ${iso.slice(0, 4)}`;
}

function scopeText(intent: QueryIntent, policy: Policy): string {
  if (intent.filters.length === 0) return "";
  return ` for ${intent.filters
    .map((f) => `${policy.dimensions[f.dimension].label.toLowerCase()} ${f.values.join(" or ")}`)
    .join(", ")}`;
}

export function narrate(
  metric: MetricDef,
  intent: QueryIntent,
  rows: OutputRow[],
  counts: SuppressionCounts,
  policy: Policy,
): string {
  const when =
    intent.grain !== "none"
      ? ` by ${intent.grain}, ${longDate(intent.timeRange.start)} to ${longDate(intent.timeRange.end)}`
      : metric.kind === "stock"
        ? ` at the end of ${longDate(intent.timeRange.end)}`
        : ` from ${intent.timeRange.start} to ${intent.timeRange.end}`;
  const by = intent.groupBy.length > 0 ? ` by ${intent.groupBy.map((d: DimensionId) => policy.dimensions[d].label.toLowerCase()).join(" and ")}` : "";
  const head = `${metric.label}${by}${scopeText(intent, policy)}${when}`;
  if (rows.length === 0) return `${head}: no matching data.`;
  const main = metric.mf_metrics[0] ?? metric.id;
  const describe = (r: OutputRow): string => {
    const parts = [...intent.groupBy.map((d) => r.dims[d] ?? ""), ...(r.period ? [periodLabel(r.period, intent.grain)] : [])];
    const value =
      metric.id === "compensation_summary"
        ? `average ${formatValue("USD", r.values["avg_base_salary"] ?? null)}, median ${formatValue("USD", r.values["median_base_salary"] ?? null)}`
        : formatValue(metric.unit, r.values[main] ?? null);
    return parts.length > 0 ? `${parts.join(", ")} ${value}` : value;
  };
  const shown = rows.slice(0, 6).map(describe);
  const more = rows.length > 6 ? `, and ${rows.length - 6} more` : "";
  const hidden = counts.primary + counts.complementary + counts.hidden_sum + counts.complement;
  const note =
    hidden > 0
      ? ` ${hidden} ${hidden === 1 ? "value is" : "values are"} hidden to protect groups of fewer than ${policy.limits.min_cohort} people.`
      : "";
  return `${head}: ${shown.join("; ")}${more}.${note} ${metric.definition}`;
}
