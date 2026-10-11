# Build progress

Tracks the SPEC.md section 19 commit plan. Updated at every commit.

## Position

Commit 9 of 17 (core cut line after commit 14).

## Done

1. `chore: scaffold repo with README, spec, license, tooling pins`
2. `feat(data): seeded synthetic HR generator with exact counts` (compensation rows pinned at 7,196)
3. `feat(dbt): staging, core and semantic models on DuckDB with count tests`
4. `feat(semantic): MetricFlow semantic models and six governed metrics`
5. `feat(policy): roles, dimensions, limits and the query catalog compiler`
6. `feat(warehouse): local DuckDB sidecar and BigQuery byte estimator`
7. `feat(worker): Hono worker, Access JWT verification, role bindings`
8. `feat(worker): policy validation, catalog binding and suppression`
9. `feat(worker): cost guard, BigQuery client and UserDO budgets`

## Next

10. `feat(worker): planner with Workers AI, OpenAI-compatible and stub providers`

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
| `uv run pytest -q` | 414 passed (includes the 312 x 3 parity sweep), about 21 s |
| `npm run typecheck` | pass |
| `npm test` (vitest in workerd via @cloudflare/vitest-plugin) | 205 passed |
| `npx wrangler deploy --env production --dry-run --outdir .wrangler/dry` (CI=true, no token) | pass; bundle 1,594 KiB (gzip 74 KiB), BigQuery catalog only |

## Deviations from SPEC.md

- Median comparison uses a rank band instead of the value-only band: gold stores the lower and upper middle values plus the sorted salaries `max(1, ceil(0.02 n))` ranks either side of them; a median passes in [band low x 0.99, band high x 1.01]. Reason: in the 312 x 3 parity sweep 14 of 17,534 DuckDB `approx_quantile` medians sat at the correct rank but up to 1.84% outside [lower middle x 0.99, upper middle x 1.01] because of gaps between adjacent salaries (example: 127,300 between ranks 64 and 65 of 129, whose values are 125,000 and 131,700). BigQuery's `APPROX_QUANTILES(x, 2)[OFFSET(1)]` returns an input value at or next to the middle rank, which the band also accepts.
- Suppression invariant 3 is stated as "suppressed cohort sum is 0, at least k, or the whole partition is suppressed": a partition whose total is below k (for example two cells of 3 and 4 and nothing else) cannot reach k, and suppressing all of it is the strongest possible outcome.
- Complement rule compares with a strict `0 < diff < k` (with a 1e-9 tolerance) rather than integer 1 to 9, so fractional cohorts (turnover's average headcount) are covered too.
- Validation codes chosen where the spec leaves them open: more than 2 group-by dimensions is `RESULT_TOO_LARGE`; more than 2 restricted (filtered or scoped) dimensions is `DIMENSION_NOT_APPROVED`; an unknown filter value or unknown metric is `OUT_OF_DOMAIN`; a disallowed grain is `GRAIN_NOT_ALLOWED` carried as a clarification with the allowed grains as options.
- `UserDO.saveTurn` returns `false` for an unknown session instead of throwing, because an exception thrown inside a Durable Object RPC method is reported as an uncaught exception by workerd.
- The BigQuery client requests `formatOptions.useInt64Timestamp` and decodes both TIMESTAMP encodings (epoch seconds in float notation and int64 microseconds); `totalBytesBilled` is read when the jobs.query response carries it, otherwise billed bytes are reported as null and the reservation stands. Both are unverified against real BigQuery until `make smoke`.
- Worker entry points are `worker/src/entry.local.ts` (bundles the DuckDB catalog) and `worker/src/entry.production.ts` (bundles the BigQuery catalog), selected by `main` per wrangler environment, because wrangler's `alias` is top-level only and cannot differ per environment. Both call `createWorker()` from `src/index.ts`.
- `src/peoplelens/policy.py`, `catalog/results.py` and `reference/tolerance.py` added (policy loader, row-to-cell mapping, tolerance rules) beyond the section 4 layout.
- Catalog JSON is about 1.45 MB per dialect (spec estimated 0.85 MB) because every entry carries a where-filter for each approved dimension of its metric.
- `turnover_rate` is `terminations / NULLIF(avg_headcount, 0)` (derived) with `avg_headcount` a ratio metric (`employee_months / snapshot_months`), so a group with no active employee-months yields NULL instead of a BigQuery division-by-zero error.
- `src/peoplelens/synth/stats.py` added (not in the section 4 layout): counts computed from the CSVs on disk, used by `pl data stats` and the count tests, independent of the generator.

- npm workspaces list only `worker` until the web UI commit (15) adds `web`, so `npm ci` works without an empty placeholder package.
- Makefile passes extra arguments through `ARGS` (for example `make bootstrap ARGS="--phase b"`), because make cannot forward flags such as `--phase` directly.
