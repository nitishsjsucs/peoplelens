# Build progress

Tracks the SPEC.md section 19 commit plan. Updated at every commit.

## Position

Commit 17 of 17 (plus inserted commits): the commit plan is complete (core cut line after commit 14). Builder 2 re-ran every check (all pass, same counts as builder 1), then reviewed the production path against the official API references, because none of it can run on this machine. Fixes from that review are commits 18 onward.

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
10. `feat(worker): planner with Workers AI, OpenAI-compatible and stub providers`
10a. `feat(dev): local stack orchestrator, local identities and pl ask/chat` (inserted: the eval runner needs it)
11. `feat(eval): 200-item eval set, gold, comparator and text-to-SQL baseline`
12. `ci: python, worker and stub end-to-end jobs`
12a. `feat(worker): ground planner drafts in the question text` (inserted after dev-set prompt work with Qwen3-1.7B)
13. `feat(ops): doctor, idempotent bootstrap, deploy and smoke for Cloudflare and BigQuery`
13a. `eval: official local run with Qwen3-1.7B and the text-to-SQL baseline`
14. `docs: local eval results and production setup guide` (official stub run, README Results)
14a. `fix(worker): ask when no metric is named; short replies keep the conversation's scope`
14b. `eval: second official local run after the conversation fixes`
14c. `docs: report both official local runs; record machine load in eval reports`
15. `feat(web): React chat UI served as Worker static assets`
16. `feat(audit): metadata-only audit trail via Queues and Basin, pyiceberg locally`
17. `feat(ops): masking demo and time-travel drill`
17a. `eval: third official local run on the completed plan`; `docs: report all three official local runs; resume-claims table`; `eval: official stub run on the completed plan`
18. `fix(ops): production BigQuery, gateway-log, bq and deploy fixes from an API review` (builder 2): the eval service account's Python BigQuery calls used `Client.query`, which calls jobs.insert, and jobs.insert does not accept the `bigquery.readonly` scope (it accepts bigquery, cloud-platform and devstorage scopes only; jobs.query, jobs.get and jobs.getQueryResults accept bigquery.readonly), so smoke, parity and the production baseline would all have failed with insufficient scopes; smoke step 5 read the `/logs/{id}/request` and `/response` payload endpoints as `{result}` envelopes, so a stored payload would have passed as "absent"; logs are written asynchronously and step 5 did not wait; governance SQL now goes to `bq` as the query argument after the global flags (documented form) instead of stdin; IAM grants retry while new service accounts propagate; every deploy rebuilds the web UI (a fresh clone has no `web/dist`) and keeps the audit bindings once the audit trail is enabled; wrangler gets the account id from `.secrets/cloudflare.env` and every ops CLI runs with stdin closed so no prompt can hang; HTTP calls send an explicit user agent.
19. `fix(eval): never follow Access redirects; production health with a service token` (builder 2): the Worker client followed redirects, so an Access login redirect (rejected or missing service token) became a 200 HTML page that the eval would have scored as schema-invalid answers; it now reports the 302. The production eval checks `/api/health` with a service token (Access guards every path) and stops if it is not 200. Smoke step 4 reports a rejected role instead of crashing. README: commit the smoke record, then `make eval-prod ARGS="--official"`.
19a. `eval: fourth official local run` (builder 2): same Worker and planner code as run 3 (commit 7821669); 147/150, 20/20, 10/10, 30/30, 0 safety violations, p95 5,353 ms with other builds sharing the Mac (load average 8 at start, 5-minute average 27 at the end); baseline 33/150 again. Reported in README next to runs 1 to 3.
20. `fix(ops): bootstrap checks the Cloudflare token first; key files stay private` (builder 2): a real bootstrap built the Cloudflare client only after the Google Cloud steps, so a missing `.secrets/cloudflare.env` stopped it halfway; it now stops before any change (a dry run still prints the Google Cloud plan first). Phase A creates `.secrets/` (0700) before `gcloud ... keys create` writes into it, and sets the Worker key to 0600 like the eval key.

## Next

Plan complete. Remaining work needs the owner: the production setup (README Production setup), then `make smoke ARGS="--drills"` and `make eval-prod`, and the first CI run on GitHub after a push.

## Check status (at this commit)

| Check | Result |
|---|---|
| `uv sync --frozen`, `npm ci` | pass (131 Python packages in uv.lock) |
| `uv run pl data generate --check` | pass (seeds byte-identical to SHA256SUMS) |
| `uv run pl dbt --dialect duckdb seed` then `build` (from a clean clone, no `.dev/`) | pass: 6 seeds, 14 models, 74 data tests (12 singular count tests) |
| `uv run pl dbt --dialect bigquery_compile parse --no-partial-parse` | pass (offline, no credentials) |
| `uv run pl mf validate` | pass (0 errors, 0 warnings) |
| `uv run pl catalog build --check` | pass: 312 entries per dialect, BigQuery compiled first, then DuckDB |
| `uv run pl-eval gold --check`; frozen eval set equals a fresh generation (pytest) | pass |
| `uv run pytest -q` | 519 passed (includes the 312 x 3 parity sweep; 493 at builder 1's last commit, re-run by builder 2 before any change, plus 22 ops tests in commit 18, 3 in commit 19 and 1 in commit 20; 519 now) |
| `npm run typecheck` (worker and web) | pass |
| `npm test` (web build, web vitest, worker vitest in workerd) | web 3 passed; worker 267 passed (includes 4 x 2,000 fuzzed planner outputs and a check that every templated eval and dev question resolves to its gold period and grain) |
| `npx wrangler deploy --env production --dry-run --outdir .wrangler/dry` (CI=true, no token) | pass; 2,471 KiB (gzip 223 KiB) including the UI assets; BigQuery catalog only |
| Fresh `git clone` at commit c462b5d, every CI job's commands with `CI=true PL_STRICT=1 WRANGLER_SEND_METRICS=false` (python, worker, e2e stub eval) | pass on macOS; GitHub's ubuntu-latest run is pending the owner's push |
| `uv run pl dev up`, `pl ask`, `pl audit tail`, `pl dev down` | pass; ports 8794, 9244 and 8180 free afterwards, no process of ours left |
| Web UI in a browser against `pl dev up --llm stub` | renders, accepts a local token, answers "Headcount by location" for the HRBP scope (1,000 people across five locations) |
| `uv run pl-ops doctor --fast` on this Mac | runs; reports wrangler login, `.secrets/cloudflare.env` and gcloud as MISSING (expected: no cloud credentials here) |
| `uv run pl-ops bootstrap --gcp-project peoplelens-demo01 --phase a --dry-run` | prints the phase A plan, changes nothing, stops at the missing Cloudflare token |
| `uv run pl-eval run --llm openai_compat --official`, run 1 (commit a0cc3a6) | semantic 149/150, 0/20 clarification, 30/30 adversarial, 0 safety violations, p95 3,696 ms; baseline 33/150 |
| same, run 2 (commit f4cee41) | semantic 146/150, 20/20 clarification, 10/10 two-turn, 29/30 adversarial, 0 safety violations; p95 15,046 ms under heavy machine load; baseline 33/150 |
| same, run 3 (commit c462b5d) | semantic 147/150, 20/20 clarification, 10/10 two-turn, 30/30 adversarial, 0 safety violations; p95 5,196 ms (load average 5 at start, 69 at end); baseline 33/150 |
| same, run 4 (commit 7821669, builder 2) | semantic 147/150, 20/20 clarification, 10/10 two-turn, 30/30 adversarial, 0 safety violations; p95 5,353 ms (load average 8.1 at start, 14.9 at end, 5-minute average 27.4 at end); baseline 33/150; the same three misses as run 3 |
| `uv run pl-eval run --llm stub --assert-safety` (builder 2, report to the scratchpad, not committed) | assertions pass: 200 items, 0 violations, 148 answers |
| `uv run pl-eval run --llm stub --assert-safety --official` (commits c0271ce and 0bc320a) | assertions pass both times: 200 items, 0 violations, 148 answers (floor pinned at 143) |

## Deviations from SPEC.md

- Median comparison uses a rank band instead of the value-only band: gold stores the lower and upper middle values plus the sorted salaries `max(1, ceil(0.02 n))` ranks either side of them; a median passes in [band low x 0.99, band high x 1.01]. Reason: in the 312 x 3 parity sweep 14 of 17,534 DuckDB `approx_quantile` medians sat at the correct rank but up to 1.84% outside [lower middle x 0.99, upper middle x 1.01] because of gaps between adjacent salaries (example: 127,300 between ranks 64 and 65 of 129, whose values are 125,000 and 131,700). BigQuery's `APPROX_QUANTILES(x, 2)[OFFSET(1)]` returns an input value at or next to the middle rank, which the band also accepts.
- Suppression invariant 3 is stated as "suppressed cohort sum is 0, at least k, or the whole partition is suppressed": a partition whose total is below k (for example two cells of 3 and 4 and nothing else) cannot reach k, and suppressing all of it is the strongest possible outcome.
- Complement rule compares with a strict `0 < diff < k` (with a 1e-9 tolerance) rather than integer 1 to 9, so fractional cohorts (turnover's average headcount) are covered too.
- Validation codes chosen where the spec leaves them open: more than 2 group-by dimensions is `RESULT_TOO_LARGE`; more than 2 restricted (filtered or scoped) dimensions is `DIMENSION_NOT_APPROVED`; an unknown filter value or unknown metric is `OUT_OF_DOMAIN`; a disallowed grain is `GRAIN_NOT_ALLOWED` carried as a clarification with the allowed grains as options.
- `UserDO.saveTurn` returns `false` for an unknown session instead of throwing, because an exception thrown inside a Durable Object RPC method is reported as an uncaught exception by workerd.
- The BigQuery client requests `formatOptions.useInt64Timestamp` and decodes both TIMESTAMP encodings (epoch seconds in float notation and int64 microseconds); `totalBytesBilled` is read when the jobs.query response carries it, otherwise billed bytes are reported as null and the reservation stands. Both are unverified against real BigQuery until `make smoke`.
- Eval helpers added beyond the section 4 layout: `eval/gold.py`, `eval/suppression.py` (Python port of the Worker's suppression rules, used to derive expected suppression), `eval/safety.py` (leakage checks recomputed from the reference implementation), `eval/cli.py`.
- Answerable eval items are generated only when every gold cell has a cohort of at least 10 and nothing would be suppressed (including complement-rule suppression for filtered and HRBP-scoped questions), so most department breakdowns of workforce metrics are absent from the answerable slice (Legal is 8 at close); department breakdowns appear through HRBP scope and filters.
- Clarification options in the eval are answered with a typed reply (for example "Headcount, please."), not `choose_option`, so the second turn is LLM-independent of option labels.
- The local warehouse sidecar speaks HTTP/1.1 keep-alive and the Worker retries a dropped local connection once: with HTTP/1.0 the first stub eval hit 9 "Network connection lost" errors (workerd reuses pooled connections the server had closed).
- The DDL screening pattern was broadened from "drop table" to "drop ... table" after reading the adversarial set ("Drop the offers table"); the adversarial pass rate is therefore not a blind measurement for that item.
- Ops modules added beyond the section 4 layout: `ops/prod_eval.py` (BigQuery as the eval service account, Workers AI REST baseline) and `catalog/parity.py` (shared by the DuckDB parity sweep and `make smoke`). The text-to-SQL baseline takes a dialect: DuckDB locally, BigQuery standard SQL in production (few-shot SQL transpiled with sqlglot, TIMESTAMP literals, APPROX_QUANTILES for medians).
- Not verified against live services (no gcloud, no credentials, no Cloudflare login on the build machine): every gcloud and bq command form, the Cloudflare Access and AI Gateway request and response shapes (field names follow SPEC.md 17; those marked P or U there remain unverified), and the BigQuery REST TIMESTAMP and `totalBytesBilled` handling in the Worker. Doctor and smoke are the checks that will surface any mismatch.
- After the first official local run showed 0/20 clarifications (Qwen3-1.7B always guessed a metric) and that a short reply such as "Headcount, please." dropped the previous turn's filters, three dev-driven rules were added: the metric is grounded like the other slots (a question that names no metric gets a clarification listing the model's guess first); a short reply that only names a metric keeps the conversation's scope and period; and a model refusal (OUT_OF_DOMAIN or DIMENSION_NOT_APPROVED) of a question that names approved values but no metric becomes a clarification (PII, write, scope and metric refusals are always kept). These were validated only on new dev items (D-CL-02 to D-CL-06, D-TT-01 to D-TT-03): 60 of 60 dev expectations and 52 of 52 dev gold answers. The frozen eval set is unchanged (the templated dev pass now always draws 57 items so the shared random stream stays fixed).
- Planner grounding (`worker/src/planner/grounding.ts`): the LLM chooses the action (query, clarify, refuse) and the metric; breakdowns, filters (including "except X" complements) and grain must be supported by the question text or the conversation it follows, and the default period is enforced server-side (model dates are used only for time words the deterministic resolver cannot read). Reason, from the dev set only: Qwen3-1.7B invented breakdowns, grains and filters on 31 of 57 dev questions; with grounding the dev set scored 57 of 57 (dev runs are prompt-iteration aids, not results).
- Audit trail in production uses a generated, gitignored `worker/wrangler.audit.generated.jsonc` (adds the queue and the Basin Pipelines stream binding to env.production) because the stream id is account-specific; once `--with-audit` has recorded the stream id in `.secrets/bootstrap-state.json`, every deploy (bootstrap phases or `make deploy`) regenerates and uses that config, so a later deploy cannot drop the audit bindings. The Basin stream schema format in `infra/cloudflare/audit_stream_schema.json` (`fields` with `name`, `type`, `required`) and the pipeline SQL form (`INSERT INTO <sink> SELECT * FROM <stream>`) match the Basin Pipelines docs (checked by builder 2 on 2026-10-10); the catalog-token permissions remain unverified (SPEC.md 17.6).
- Stub eval reports never include accuracy (fields are null and the Markdown says "not reported"); they report safety, schema validity, answer counts and latency only.
- `pl-eval run --dev` runs the dev set for prompt iteration; its reports never go to `eval/results/`.
- Added `src/peoplelens/dev/client.py` (HTTP client shared by `pl ask`, `pl chat` and the eval runner) and `tests/py/test_dev_tokens.py`.
- The request pipeline lives in `worker/src/ask.ts` (not in the section 4 layout) so `index.ts` stays a thin router and the fuzz test can drive the pipeline without HTTP.
- Deterministic request screening (`planner/values.ts`) refuses write requests, person-level or PII requests, unapproved or sensitive attributes and access-escalation attempts before the LLM is called; resolved time phrases and explicit grain words override the model's dates and grain. Both are server-side and LLM-independent.
- The planner's clarification options carry a metric only (`{label, metric}`); a policy clarification (disallowed grain) carries a grain. A typed reply to a clarification is merged with the pending draft; `choose_option` resolves without an LLM call.
- Refusal messages never echo planner-supplied strings (dimension names or values), only policy labels.
- `policy/dimensions.json`: removed the aliases "people" (it collides with "how many people") and "new hires" (it collides with recruiting language).
- Worker entry points are `worker/src/entry.local.ts` (bundles the DuckDB catalog) and `worker/src/entry.production.ts` (bundles the BigQuery catalog), selected by `main` per wrangler environment, because wrangler's `alias` is top-level only and cannot differ per environment. Both call `createWorker()` from `src/index.ts`.
- `src/peoplelens/policy.py`, `catalog/results.py` and `reference/tolerance.py` added (policy loader, row-to-cell mapping, tolerance rules) beyond the section 4 layout.
- Catalog JSON is about 1.45 MB per dialect (spec estimated 0.85 MB) because every entry carries a where-filter for each approved dimension of its metric.
- `turnover_rate` is `terminations / NULLIF(avg_headcount, 0)` (derived) with `avg_headcount` a ratio metric (`employee_months / snapshot_months`), so a group with no active employee-months yields NULL instead of a BigQuery division-by-zero error.
- `src/peoplelens/synth/stats.py` added (not in the section 4 layout): counts computed from the CSVs on disk, used by `pl data stats` and the count tests, independent of the generator.
- The root `npm test` builds the web UI first, because the Worker's static-assets directory (`web/dist`) must exist for the vitest pool and `wrangler deploy`; `pl dev up` builds it when missing.
- Makefile passes extra arguments through `ARGS` (for example `make bootstrap ARGS="--phase b"`), because make cannot forward flags such as `--phase` directly.
- Builder 2: the production eval and smoke query BigQuery through jobs.query (`Client.query_and_wait`) and dry-run through a direct jobs.query REST call, never `Client.query` (jobs.insert), so the eval service account keeps the `bigquery.readonly` scope like the Worker. Smoke reports bytes processed, not billed (jobs.query responses do not reliably carry billed bytes).
- Builder 2: SPEC.md 17.6 step 2 pipes the governance SQL into `bq query` on stdin. The bootstrap now runs `bq --project_id=<p> --location=US query --use_legacy_sql=false '<sql>'` (global flags before the command, SQL as the documented query argument, comment lines removed so the argument cannot start with `--`).
- Builder 2: smoke step 5 is stricter than SPEC.md 17.8: besides custom metadata and empty `/request` and `/response` payloads, it fails when the log's `request_head` or `response_head` is non-empty or the question text appears anywhere in the log, and it reports a payload endpoint that answers with an unexpected status as unknown (a fail with a pointer to the dashboard), never as absent.

## Builder 2 verification log (2026-10-10, official docs and local `--help`)

| Fact | Source | Result |
|---|---|---|
| OAuth scopes per BigQuery method | REST reference pages for jobs.insert, jobs.query, jobs.get, jobs.getQueryResults | jobs.insert: bigquery, cloud-platform, devstorage.* only; the other three also accept bigquery.readonly and cloud-platform.read-only. Led to the commit 18 fix |
| google-cloud-bigquery 3.46.1 query paths | installed source (`client.py`, `_job_helpers.py`) | `Client.query` defaults to jobs.insert; `query_and_wait` uses jobs.query when every request key is in its allowlist (ours are) |
| AI Gateway log detail | API reference, Get Gateway Log Detail | `metadata` is a string; `request_head`, `response_head` (a stored request body appears there, per a published example) and their `_complete` flags exist |
| AI Gateway log request payload | API reference, Get Gateway Log Request | returns "the original request payload"; no envelope is documented. Led to the commit 18 smoke fix |
| Access application-scoped policy create | API reference | still documented (the page recommends reusable policies); decision values allow, deny, non_identity, bypass |
| Workers Get Worker | API reference (Workers beta) | `worker_id` may be the name; `id` is a 32-hex string, the same shape as the Access `worker_id` example |
| JWT header behind Access on a Worker | 2025-10-03 changelog, one-click Access for Workers | Access adds `Cf-Access-Jwt-Assertion`, and the Worker should still validate it (as this Worker does) |
| Workers AI llama-3.3-70b-instruct-fp8-fast | model page | live, no deprecation notice; $0.293 and $2.253 per M input and output tokens; 24,000-token context |
| Basin stream schema and pipeline SQL | Pipelines docs (manage streams, R2 SQL tutorial); `wrangler basin pipelines ... --help` (4.149.0) | schema file format and `INSERT INTO <sink> SELECT * FROM <stream>` match; `--http-enabled`, `--sql`, sink `--catalog-token` flags exist |
| bq flag placement | bq command-line reference | documented form is `bq [global flags] COMMAND [command flags] [args]` |

