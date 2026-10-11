// Small-cohort suppression (pure). SPEC.md section 9.5.
//  1. Primary: a cell whose cohort is below k is suppressed.
//  2. Complementary: in every partition of cells that share all but one dimension
//     value (and the time bucket), a single suppressed cell drags the smallest
//     unsuppressed cell with it.
//  3. Hidden sum: in every partition the suppressed cohorts must sum to 0 or at
//     least k (or the whole partition is suppressed); otherwise the next smallest
//     cell is suppressed. Rules 2 and 3 repeat to a fixed point.
//  4. Complement rule: cells forced by guard/complement.ts are suppressed first.
// Cohort values never leave this module: output rows carry only `suppressed`.
import type { ResultSet } from "../warehouse/types";

export interface Cell {
  key: string;
  dims: Record<string, string>;
  time: string | null;
  values: Record<string, number | null>;
  cohort: number;
}

export type Reason = "primary" | "complementary" | "hidden_sum" | "complement";

export interface SuppressionCounts {
  primary: number;
  complementary: number;
  hidden_sum: number;
  complement: number;
}

export interface OutputRow {
  dims: Record<string, string>;
  period: string | null;
  values: Record<string, number | null>;
  suppressed: boolean;
}

export interface SuppressedResult {
  rows: OutputRow[];
  counts: SuppressionCounts;
  suppressedKeys: Set<string>;
}

export interface SuppressSpec {
  dims: string[]; // dimension ids, in group-by order
  k: number;
  forced?: ReadonlySet<string>; // complement-rule keys
}

export function cellKey(dims: Record<string, string>, order: readonly string[], time: string | null): string {
  return JSON.stringify([...order.map((d) => dims[d] ?? null), time]);
}

function toNumber(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Normalize a warehouse result into cells, using the catalog column map. */
export function toCells(
  rs: ResultSet,
  map: { dimensions: Record<string, string>; time: string | null; values: string[]; cohort: string },
  dimOrder: readonly string[],
): Cell[] {
  const idx = new Map(rs.columns.map((c, i) => [c, i] as const));
  const col = (name: string): number => {
    const i = idx.get(name);
    if (i === undefined) throw new Error(`result is missing column ${name}`);
    return i;
  };
  const cells: Cell[] = [];
  for (const row of rs.rows) {
    const cohortRaw = row[col(map.cohort)];
    // An aggregate over zero rows (no group-by) comes back as one all-NULL row: no cell.
    if (cohortRaw === null || cohortRaw === undefined) continue;
    const dims: Record<string, string> = {};
    for (const d of dimOrder) {
      const column = map.dimensions[d];
      if (column === undefined) throw new Error(`no column for dimension ${d}`);
      dims[d] = String(row[col(column)]);
    }
    const timeRaw = map.time ? row[col(map.time)] : null;
    const time = timeRaw === null || timeRaw === undefined ? null : String(timeRaw).slice(0, 10);
    const values: Record<string, number | null> = {};
    for (const m of map.values) values[m] = toNumber(row[col(m)]);
    cells.push({ key: cellKey(dims, dimOrder, time), dims, time, values, cohort: toNumber(cohortRaw) ?? 0 });
  }
  return cells;
}

const EPS = 1e-9;

export function suppressCells(cells: readonly Cell[], spec: SuppressSpec): SuppressedResult {
  const reason = new Map<string, Reason>();
  for (const c of cells) {
    if (c.cohort < spec.k - EPS) reason.set(c.key, "primary");
    else if (spec.forced?.has(c.key)) reason.set(c.key, "complement");
  }
  const byCohort = (a: Cell, b: Cell) => a.cohort - b.cohort || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);

  let changed = true;
  while (changed) {
    changed = false;
    for (const d of spec.dims) {
      const partitions = new Map<string, Cell[]>();
      for (const c of cells) {
        const pk = JSON.stringify([...spec.dims.filter((x) => x !== d).map((x) => c.dims[x] ?? null), c.time]);
        const list = partitions.get(pk);
        if (list) list.push(c);
        else partitions.set(pk, [c]);
      }
      for (const pk of [...partitions.keys()].sort()) {
        const group = partitions.get(pk) ?? [];
        if (group.length <= 1) continue;
        const suppressed = group.filter((c) => reason.has(c.key));
        const open = group.filter((c) => !reason.has(c.key)).sort(byCohort);
        const first = open[0];
        if (suppressed.length === 0 || !first) continue;
        if (suppressed.length === 1) {
          reason.set(first.key, "complementary");
          changed = true;
          continue;
        }
        const hidden = suppressed.reduce((s, c) => s + c.cohort, 0);
        if (hidden > EPS && hidden < spec.k - EPS) {
          reason.set(first.key, "hidden_sum");
          changed = true;
        }
      }
    }
  }

  const counts: SuppressionCounts = { primary: 0, complementary: 0, hidden_sum: 0, complement: 0 };
  for (const r of reason.values()) counts[r] += 1;
  const rows: OutputRow[] = cells.map((c) => {
    const isSuppressed = reason.has(c.key);
    const values: Record<string, number | null> = {};
    for (const [m, v] of Object.entries(c.values)) values[m] = isSuppressed ? null : v;
    return { dims: { ...c.dims }, period: c.time, values, suppressed: isSuppressed };
  });
  return { rows, counts, suppressedKeys: new Set(reason.keys()) };
}

/** SPEC 9.2 signature: suppress a raw result set. */
export function suppress(
  rs: ResultSet,
  spec: {
    dims: string[];
    dimColumns: Record<string, string>;
    time?: string | null;
    measures: string[];
    cohort: string;
    k: number;
    forced?: ReadonlySet<string>;
  },
): SuppressedResult {
  const cells = toCells(
    rs,
    { dimensions: spec.dimColumns, time: spec.time ?? null, values: spec.measures, cohort: spec.cohort },
    spec.dims,
  );
  return suppressCells(cells, { dims: spec.dims, k: spec.k, ...(spec.forced ? { forced: spec.forced } : {}) });
}
