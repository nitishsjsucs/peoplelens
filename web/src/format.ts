// Display formatting for answer cells. Suppressed cells never carry a value.
import type { Column, Row } from "./api";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function formatMeasure(unit: string | undefined, value: unknown): string {
  if (value === null || value === undefined) return "hidden";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  switch (unit) {
    case "employees":
      return Math.round(n).toLocaleString("en-US");
    case "FTE":
      return n.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    case "ratio":
      return `${(n * 100).toFixed(1)}%`;
    case "days":
      return `${n.toFixed(1)} d`;
    case "USD":
      return `$${Math.round(n).toLocaleString("en-US")}`;
    default:
      return String(n);
  }
}

export function formatPeriod(value: unknown, grain: string | undefined): string {
  if (typeof value !== "string") return "";
  const y = value.slice(0, 4);
  const m = Number(value.slice(5, 7));
  if (grain === "year") return y;
  if (grain === "quarter") return `Q${Math.floor((m - 1) / 3) + 1} ${y}`;
  return `${MONTHS[m - 1] ?? ""} ${y}`;
}

export function formatCell(column: Column, row: Row, grain?: string): string {
  const v = row[column.name];
  if (column.kind === "measure") return row["suppressed"] === true ? "hidden" : formatMeasure(column.unit, v);
  if (column.kind === "time") return formatPeriod(v, grain);
  return v === null || v === undefined ? "" : String(v);
}

export function hiddenCount(s: { primary: number; complementary: number; hidden_sum: number; complement: number }): number {
  return s.primary + s.complementary + s.hidden_sum + s.complement;
}
