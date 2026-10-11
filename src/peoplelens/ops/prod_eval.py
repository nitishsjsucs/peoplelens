"""Production helpers for smoke and the production eval: BigQuery as the
peoplelens-eval service account, and the Workers AI REST baseline client."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from peoplelens.ops.checks import CLOUDFLARE_ENV, EVAL_KEY, load_state, read_env_file

READONLY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"
PLACEHOLDER = "`peoplelens-project-id`"
BASELINE_MAX_BYTES = 200 * 1024 * 1024
MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"


def bq_client(project: str, key_path=EVAL_KEY):
    from google.cloud import bigquery
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(str(key_path), scopes=[READONLY_SCOPE])
    return bigquery.Client(project=project, credentials=creds, location="US")


def query_parameters(values: dict) -> list:
    from google.cloud import bigquery

    out = []
    for name, v in values.items():
        if isinstance(v, date):
            out.append(bigquery.ScalarQueryParameter(name, "TIMESTAMP", datetime(v.year, v.month, v.day, tzinfo=timezone.utc)))
        else:
            out.append(bigquery.ArrayQueryParameter(name, "STRING", list(v)))
    return out


def run_entry(client, entry: dict, values: dict, project: str, *, dry_run: bool = False,
              max_bytes: int | None = None, use_cache: bool = True):
    """Execute (or dry-run) one BigQuery catalog entry. Returns (columns, rows, bytes)."""
    from google.cloud import bigquery

    sql = entry["sql"].replace(PLACEHOLDER, f"`{project}`")
    cfg = bigquery.QueryJobConfig(
        query_parameters=query_parameters(values),
        dry_run=dry_run,
        use_query_cache=use_cache,
        labels={"app": "peoplelens", "purpose": "smoke"},
    )
    if max_bytes is not None:
        cfg.maximum_bytes_billed = max_bytes
    job = client.query(sql, job_config=cfg)
    if dry_run:
        return [], [], job.total_bytes_processed
    result = job.result(timeout=60)
    columns = [f.name for f in result.schema]
    rows = [tuple(r.values()) for r in result]
    return columns, rows, job.total_bytes_billed


class BigQueryExecutor:
    """Text-to-SQL baseline executor in production: eval service account, 200 MiB cap."""

    kind = "bigquery"
    dialect = "bigquery"

    def __init__(self, project: str):
        from google.cloud import bigquery

        self.project = project
        self.client = bq_client(project)
        self.cfg = bigquery.QueryJobConfig(
            default_dataset=f"{project}.peoplelens_semantic",
            maximum_bytes_billed=BASELINE_MAX_BYTES,
            labels={"app": "peoplelens", "purpose": "baseline"},
        )

    def run(self, sql: str):
        result = self.client.query(sql, job_config=self.cfg).result(timeout=60)
        return [f.name for f in result.schema], [list(r.values()) for r in result]

    def close(self) -> None:
        self.client.close()


def prod_baseline():
    from peoplelens.eval.llm_client import WorkersAiRestSql

    env = read_env_file(CLOUDFLARE_ENV)
    state = load_state()
    if not env.get("CLOUDFLARE_ACCOUNT_ID") or not env.get("CLOUDFLARE_API_TOKEN") or not state.get("gcp_project"):
        raise SystemExit("prod baseline needs .secrets/cloudflare.env and a bootstrapped project")
    return WorkersAiRestSql(env["CLOUDFLARE_ACCOUNT_ID"], env["CLOUDFLARE_API_TOKEN"], MODEL), BigQueryExecutor(state["gcp_project"])


def dumps(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n"
