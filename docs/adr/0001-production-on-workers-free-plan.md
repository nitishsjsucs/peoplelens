---
status: accepted
date: 2026-10-10
---

# Production runs entirely on the Cloudflare Workers Free plan

Nitish chose to keep his Cloudflare account on Workers Free, and that account is shared with three other repos. So PeopleLens production must not depend on any Workers Paid feature, and it must stay inside the account-wide daily Free allowances with margin. Two parts of the v1 design broke that rule. First, the optional audit trail used Basin Pipelines, which is Paid only (`This feature requires a Workers Paid plan [code: 1021]` on his account). Second, the production eval needed about 31,000 Workers AI neurons (about 61,000 at the upper bound) against an allowance of 10,000 per account per UTC day, and on Free that allowance cannot be extended by paying. Instead of upgrading, we redesigned both parts and kept everything else. SPEC.md section 25 records the full audit, the measurements and the usage budget.

## Decisions

- **Audit ingestion**: Worker, then Queue `peoplelens-audit`, then the same Worker's queue consumer, which writes each batch as one immutable, content-addressed NDJSON object to R2 (`staging/v1/dt=<day>/<sha256 of the batch's request ids>.ndjson`). A Python committer (`pl-ops audit commit`, pyiceberg `RestCatalog` against Basin Catalog) appends the staged rows to the Iceberg table `peoplelens.audit_events`. It deduplicates on `request_id`, so delivery stays at least once while the table effect is exactly once. Locally, the same committer runs against a staging directory and a pyiceberg `SqlCatalog`. If the queue's daily share is used up, the Worker writes the event straight to R2 (`direct-<request_id>.ndjson`), so a Free Queue limit can never drop an event.
- **Workers AI**: the planner model stays `@cf/meta/llama-3.3-70b-instruct-fp8-fast`. It is the only current model on Cloudflare's JSON Mode list and is not on the paid-billing list. A new SQLite Durable Object, `AccountBudgetDO` (one instance), holds this repo's daily share of account-wide allowances: 6,000 neurons and 800 audit queue messages per UTC day. (Superseded on 2026-10-11 by the fixer round, PROGRESS.md item 42: 400 audit queue messages with 3 retries, because every retry is another queue read, plus a third counter capping direct R2 audit puts at 2,000 a day.) The Worker reserves budget before each Workers AI call and settles it against the returned token usage. Workers AI error 3036 (the daily free allocation is used up) becomes the refusal `AI_DAILY_LIMIT`, with no retry.
- **Production eval**: the eval is resumable and budget-capped. It checkpoints per item, spends at most 5,000 neurons per UTC day on the semantic path and 2,000 on the baseline, and stops cleanly on `AI_DAILY_LIMIT`. A full run takes about 5 UTC days (at most 18) on one commit and one deployed build. Every reported number names the model that produced it.

## Considered options

- **Upgrade to Workers Paid ($5/month).** Rejected: Nitish's decision. It would also hide the account-wide limits that the other repos share.
- **Switch the planner to a cheaper model so that one eval fits in a day.** Rejected for now. At these input-heavy prompts, `llama-3.1-8b-instruct-fp8` costs about 51 neurons per call instead of about 110, which still does not fit one eval into a day. The cheaper current models (granite-4.0-h-micro, glm-4.7-flash, qwen3-30b-a3b-fp8) are not on the JSON Mode list. Changing the model would also change the measured product. This stays the documented fallback.
- **Write audit events straight to R2 from the request and drop the Queue.** Rejected as the main path because the Queue gives retries outside the request. The direct put is kept as the overflow path.
- **Commit Iceberg from the Worker.** Rejected: writing Parquet and Iceberg metadata does not fit in 10 ms of CPU, and the repo already uses pyiceberg.

## Consequences

- Audit rows reach Iceberg only when the committer runs from the operator's machine. R2 staging is durable in the meantime, so the 24 hour Free queue retention only matters while the consumer is failing.
- The Basin Catalog token must be an R2 API token with Admin Read & Write, which covers every bucket in the shared account. It lives only in `.secrets/`, never in the Worker.
- On days when the production eval runs, PeopleLens may use up to 8,000 of the account's 10,000 neurons. Schedule eval days with the other repos.
- The first one or two requests on a cold isolate measured 11 to 16 ms of CPU in Node, above the Free 10 ms. They rely on the per-isolate flexibility for infrequent overruns that Cloudflare documents. Smoke counts `exceededCpu` outcomes so that a consistent overrun is caught.
