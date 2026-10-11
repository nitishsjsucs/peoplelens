// The compiled MetricFlow catalog (worker/src/generated/catalog.<dialect>.json),
// written by `pl catalog build`. The local build bundles the DuckDB catalog and the
// production build the BigQuery catalog (entry.local.ts / entry.production.ts).
import type { DimensionId, Grain, MetricId } from "../policy/types";

export interface CatalogParam {
  name: string;
  type: "DATE" | "VARCHAR[]" | "TIMESTAMP" | "ARRAY<STRING>";
}

export interface CatalogEntry {
  key: string;
  metric: MetricId;
  group_by: DimensionId[];
  grain: Grain;
  mf_metrics: string[];
  columns: {
    dimensions: Partial<Record<DimensionId, string>>;
    time: string | null;
    values: string[];
    cohort: string;
  };
  params: CatalogParam[];
  sql: string;
  sha256: string;
  row_bound: { dims: DimensionId[]; grain: Grain };
}

export interface CatalogFile {
  header: {
    dialect: "duckdb" | "bigquery";
    entries: number;
    metricflow: string;
    dbt_metricflow: string;
    dbt_core: string;
    manifest_sha256: string;
    project_placeholder: string | null;
  };
  entries: Record<string, CatalogEntry>;
}

/** `metric|sorted(group_by)|grain`, identical to peoplelens.catalog.keys.catalog_key. */
export function catalogKey(v: { metric: MetricId; groupBy: readonly DimensionId[]; grain: Grain }): string {
  return `${v.metric}|${[...v.groupBy].sort().join(",")}|${v.grain}`;
}

export function lookupEntry(catalog: CatalogFile, key: string): CatalogEntry | undefined {
  return Object.prototype.hasOwnProperty.call(catalog.entries, key) ? catalog.entries[key] : undefined;
}
