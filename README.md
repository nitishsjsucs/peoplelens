# PeopleLens

Governed AI workforce analytics agent. A Cloudflare Worker turns a natural-language workforce question into a structured query intent with one LLM call, checks it against a role policy, and executes only SQL that MetricFlow generated ahead of time from a governed dbt semantic layer. Results pass through small-cohort suppression before anyone sees them. All data is synthetic and seeded (fictional company "Lumen Works").

The design contract is [SPEC.md](SPEC.md). Build progress is tracked in [PROGRESS.md](PROGRESS.md).

## Status

v1 is being built from scratch, commit by commit, following the plan in SPEC.md section 19. This table is updated in every commit that changes status.

| Area | Status |
|---|---|
| Seeded synthetic HR data with exact counts | planned |
| dbt models on DuckDB (local) and BigQuery (production) | planned |
| MetricFlow semantic layer, six governed metrics | planned |
| Policy (four roles, five approved dimensions, limits) and compiled query catalog | planned |
| Local DuckDB warehouse sidecar and byte estimator | planned |
| Worker: Access JWT verification and role bindings | planned |
| Worker: policy validation, catalog binding, suppression (k = 10) | planned |
| Worker: cost guard, BigQuery client, per-user budgets | planned |
| Worker: planner (Workers AI, OpenAI-compatible local, stub) | planned |
| Eval harness (200 items, text-to-SQL baseline) | planned |
| CI (python, worker, stub end-to-end) | planned |
| Ops: doctor, phased bootstrap, deploy, smoke | planned |
| Results | none yet |

Nothing has been deployed to Cloudflare or run against BigQuery from this repo yet.

## Architecture

### Production

```mermaid
flowchart LR
  UI[Browser, pl CLI or eval harness] -->|HTTPS| ACC[Cloudflare Access]
  ACC -->|Cf-Access-Jwt-Assertion| W[Worker peoplelens, Hono]
  W --> AUTH[Verify RS256 JWT against team JWKS]
  AUTH --> RB[Role binding]
  W <--> DO[(UserDO, SQLite Durable Object:<br/>sessions, budgets, rate limit)]
  W --> PLAN[Planner]
  PLAN -->|metadata-only logging| GW[AI Gateway peoplelens]
  GW --> WAI[Workers AI llama-3.3-70b, JSON mode]
  PLAN --> POL[Policy engine]
  POL --> CAT[(Compiled MetricFlow catalog,<br/>BigQuery dialect)]
  CAT --> COST[Cost guard: row bound, dry run,<br/>maximumBytesBilled, daily budget]
  COST --> BQ[(BigQuery authorized views)]
  BQ --> SUP[Suppression k=10 plus complement rule]
  SUP --> NAR[Deterministic narrative]
  subgraph BuildTime[Build time]
    GEN[Seeded generator] --> DBT[dbt models and MetricFlow semantic models]
    DBT --> MFC[MetricFlow compiler]
    MFC --> CAT
  end
```

### Local and CI

```mermaid
flowchart LR
  CLI[pl CLI or eval harness] -->|JWT minted with a local RS256 key| WD[wrangler dev --local :8794]
  WD --> AUTHL[Static local JWKS, loopback only]
  WD --> DOL[(UserDO in Miniflare)]
  WD -->|openai_compat| LS[llama-server :8180, Qwen3-1.7B]
  WD -->|stub, CI| STUB[Deterministic stub planner]
  WD --> SC[pl warehouse serve, 127.0.0.1 ephemeral port]
  SC --> DDB[(DuckDB file, read only)]
```

## What runs where

Local pieces are stand-ins. DuckDB is not BigQuery, llama-server is not Workers AI, and locally minted keys are not Cloudflare Access.

| Piece | Local and CI | Production | Status |
|---|---|---|---|
| Worker API | wrangler dev --local on 8794 | Cloudflare Workers (`peoplelens`) | planned |
| Identity | local RS256 keys, static JWKS | Cloudflare Access, team JWKS | planned |
| LLM | Qwen3-1.7B via llama-server, or a deterministic stub | Workers AI llama-3.3-70b via AI Gateway | planned |
| Semantic layer | MetricFlow, DuckDB dialect | MetricFlow, BigQuery dialect | planned |
| Warehouse | DuckDB file (dbt-duckdb) | BigQuery | planned |
| Cost limits | estimated bytes | BigQuery dry run plus maximumBytesBilled | planned |
| Suppression and complement rule | same Worker code | same Worker code | planned |
| Authorized views | plain DuckDB views | BigQuery authorized views | planned |
| Column security, masking | not emulated | BigQuery policy tags and data policies | planned, optional |
| Time travel and recovery | not emulated | dataset time-travel window | planned |
| Audit trail (Queues, Basin Pipelines, Basin Catalog) | pyiceberg SqlCatalog | Queues plus Basin Pipelines plus Basin Catalog | not built |
| Cloudflare Workflows | not used | not used | not used |

Cloudflare Workflows: not used. Nothing in this product is a long-running durable multi-step process.

## Local setup

Requirements: Node 24 or newer, npm, uv 0.12.17, GNU make (Xcode CLT ships GNU make 3.81).

```bash
make setup      # uv sync --frozen, npm ci
```

Further local commands (`make data`, `make dbt`, `make catalog`, `make dev-up`, `make eval-stub`) are added as their commits land.

## Production setup

Production needs a Cloudflare account (Workers Paid for the production eval), a Zero Trust organization, and a Google Cloud project with billing. The full guide, including the doctor and the idempotent bootstrap, lands with the ops commit. Until then see SPEC.md section 17.

## Results

No results yet. This section will contain only numbers produced by this repo's scripts, each with its command and date.

## License

MIT
