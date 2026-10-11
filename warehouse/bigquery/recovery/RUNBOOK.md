# BigQuery recovery runbook

PeopleLens data is synthetic and fully reproducible (`make data` and `make dbt` rebuild everything), so recovery is about speed, not data loss. This runbook covers the BigQuery-native paths.

## Windows

| Dataset | Time-travel window | After the window |
|---|---|---|
| `peoplelens_raw` | 48 hours | 7-day fail-safe, recoverable only through Google Cloud support |
| `peoplelens_staging`, `peoplelens_core`, `peoplelens_semantic` | 168 hours (the maximum) | same fail-safe |

The windows are set by `warehouse/bigquery/governance/01_datasets.sql` (bootstrap phase A) and checked by `make doctor`.

## Restore a table to a point in time (inside the window)

1. Pick a UTC timestamp before the bad change.
2. Render `warehouse/bigquery/recovery/time_travel_restore.sql` and run it:
   ```bash
   bq query --use_legacy_sql=false < restore.sql
   ```
   It creates `<table>_restored_<suffix>` with `FOR SYSTEM_TIME AS OF`; it never overwrites the original.
3. Compare row counts with the dbt singular tests (`uv run pl dbt --dialect bigquery test`), then swap names with `ALTER TABLE ... RENAME TO`.

## Restore a deleted table (inside the window)

```bash
bq cp "<project>:peoplelens_core.fct_workforce_monthly@<epoch-millis>" "<project>:peoplelens_core.fct_workforce_monthly_restored"
```

`<epoch-millis>` is a time before the deletion, in milliseconds since the epoch.

## Rebuild from seeds (always works)

```bash
GCP_PROJECT_ID=<project> uv run pl dbt --dialect bigquery seed
GCP_PROJECT_ID=<project> uv run pl dbt --dialect bigquery build
```

Then re-run `make smoke` to confirm the 312 catalog entries still match gold.

`make smoke ARGS="--drills"` runs a recovery drill against a live project: it copies `dim_month` to a scratch table, deletes every row, restores it with `FOR SYSTEM_TIME AS OF` into a new table, checks the row count and drops both scratch tables. It has not been run yet (no Google Cloud project on the build machine).
