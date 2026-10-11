import type { BoundQuery } from "../catalog/bind";

export type CellValue = string | number | null;

export interface ResultSet {
  columns: string[];
  rows: CellValue[][];
}

export interface RunOptions {
  maxBytesBilled: number;
  timeoutMs: number;
  labels: Record<string, string>;
}

export interface RunResult extends ResultSet {
  billedBytes: number | null;
  elapsedMs: number;
}

export interface Warehouse {
  readonly kind: "bigquery" | "duckdb-local" | "fake";
  dryRun(q: BoundQuery): Promise<{ estimatedBytes: number }>;
  run(q: BoundQuery, o: RunOptions): Promise<RunResult>;
}
