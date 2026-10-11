# Build progress

Tracks the SPEC.md section 19 commit plan. Updated at every commit.

## Position

Commit 5 of 17 (core cut line after commit 14).

## Done

1. `chore: scaffold repo with README, spec, license, tooling pins`
2. `feat(data): seeded synthetic HR generator with exact counts` (compensation rows pinned at 7,196)
3. `feat(dbt): staging, core and semantic models on DuckDB with count tests`
4. `feat(semantic): MetricFlow semantic models and six governed metrics`
5. `feat(policy): roles, dimensions, limits and the query catalog compiler`

## Next

6. `feat(warehouse): local DuckDB sidecar and BigQuery byte estimator`

## Check status (at this commit)

| Check | Result |
|---|---|
| `uv sync --frozen` | pass (131 packages in uv.lock) |
| `npm ci` | pass |
| `uv run pl data generate --check` | pass |
| `uv run pl dbt --dialect duckdb seed` then `build` (from a clean clone, no `.dev/`) | pass: 6 seeds, 14 models, 74 data tests (12 singular count tests) |
| `uv run pl dbt --dialect bigquery_compile parse --no-partial-parse` | pass (offline, no credentials) |
| `uv run pl mf validate` | pass (0 errors, 0 warnings) |
| `mf query` sample (headcount and fte by department, Dec 2025; turnover, offer acceptance, time to fill by year) | matches the reference implementation |
| `uv run pl catalog build --check` | pass: 312 entries per dialect, BigQuery compiled first, then DuckDB, about 23 s |
| `uv run pytest -q` | 390 passed (includes the 312 x 3 parity sweep), about 21 s |

## Deviations from SPEC.md

- Median comparison uses a rank band instead of the value-only band: gold stores the lower and upper middle values plus the sorted salaries `max(1, ceil(0.02 n))` ranks either side of them; a median passes in [band low x 0.99, band high x 1.01]. Reason: in the 312 x 3 parity sweep 14 of 17,534 DuckDB `approx_quantile` medians sat at the correct rank but up to 1.84% outside [lower middle x 0.99, upper middle x 1.01] because of gaps between adjacent salaries (example: 127,300 between ranks 64 and 65 of 129, whose values are 125,000 and 131,700). BigQuery's `APPROX_QUANTILES(x, 2)[OFFSET(1)]` returns an input value at or next to the middle rank, which the band also accepts.
- `src/peoplelens/policy.py`, `catalog/results.py` and `reference/tolerance.py` added (policy loader, row-to-cell mapping, tolerance rules) beyond the section 4 layout.
- Catalog JSON is about 1.45 MB per dialect (spec estimated 0.85 MB) because every entry carries a where-filter for each approved dimension of its metric.
- `turnover_rate` is `terminations / NULLIF(avg_headcount, 0)` (derived) with `avg_headcount` a ratio metric (`employee_months / snapshot_months`), so a group with no active employee-months yields NULL instead of a BigQuery division-by-zero error.
- `src/peoplelens/synth/stats.py` added (not in the section 4 layout): counts computed from the CSVs on disk, used by `pl data stats` and the count tests, independent of the generator.

- npm workspaces list only `worker` until the web UI commit (15) adds `web`, so `npm ci` works without an empty placeholder package.
- Makefile passes extra arguments through `ARGS` (for example `make bootstrap ARGS="--phase b"`), because make cannot forward flags such as `--phase` directly.
