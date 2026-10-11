# Build progress

Tracks the SPEC.md section 19 commit plan. Updated at every commit.

## Position

Commit 4 of 17 (core cut line after commit 14).

## Done

1. `chore: scaffold repo with README, spec, license, tooling pins`
2. `feat(data): seeded synthetic HR generator with exact counts` (compensation rows pinned at 7,196)
3. `feat(dbt): staging, core and semantic models on DuckDB with count tests`
4. `feat(semantic): MetricFlow semantic models and six governed metrics`

## Next

5. `feat(policy): roles, dimensions, limits and the query catalog compiler`

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
| `uv run pytest -q` | 48 passed |

## Deviations from SPEC.md

- `turnover_rate` is `terminations / NULLIF(avg_headcount, 0)` (derived) with `avg_headcount` a ratio metric (`employee_months / snapshot_months`), so a group with no active employee-months yields NULL instead of a BigQuery division-by-zero error.
- `src/peoplelens/synth/stats.py` added (not in the section 4 layout): counts computed from the CSVs on disk, used by `pl data stats` and the count tests, independent of the generator.

- npm workspaces list only `worker` until the web UI commit (15) adds `web`, so `npm ci` works without an empty placeholder package.
- Makefile passes extra arguments through `ARGS` (for example `make bootstrap ARGS="--phase b"`), because make cannot forward flags such as `--phase` directly.
