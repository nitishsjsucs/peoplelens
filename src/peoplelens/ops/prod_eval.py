"""Production helpers for smoke and the production eval: BigQuery as the
peoplelens-eval service account, and the Workers AI REST baseline client.

The eval service account's credentials carry only the bigquery.readonly scope,
like the Worker's. That scope is accepted by jobs.query, jobs.get and
jobs.getQueryResults but not by jobs.insert (checked in the BigQuery REST
reference on 2026-10-10), so queries go through `Client.query_and_wait` (which
uses jobs.query for these job configurations) and dry runs are a direct
jobs.query call. `Client.query` must not be used here: it calls jobs.insert and
would fail with insufficient authentication scopes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

from peoplelens.ops.checks import CLOUDFLARE_ENV, EVAL_KEY, load_state, read_env_file

READONLY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"
BQ_API = "https://bigquery.googleapis.com/bigquery/v2"
PLACEHOLDER = "`peoplelens-project-id`"
BASELINE_MAX_BYTES = 200 * 1024 * 1024
# Shared Google Cloud project (SPEC.md 25.4 E): every executed job carries a byte cap.
PARITY_MAX_BYTES = 200 * 1024 * 1024
MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
LOCATION = "US"


class BigQueryError(RuntimeError):
    pass


@dataclass
class EvalBigQuery:
    """The eval service account's BigQuery client plus an authorized HTTP session (same credentials)."""

    project: str
    client: object
    session: object

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            close()


def bq_client(project: str, key_path=EVAL_KEY) -> EvalBigQuery:
    from google.auth.transport.requests import AuthorizedSession
    from google.cloud import bigquery
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(str(key_path), scopes=[READONLY_SCOPE])
    return EvalBigQuery(project, bigquery.Client(project=project, credentials=creds, location=LOCATION), AuthorizedSession(creds))


def query_parameters(values: dict) -> list:
    from google.cloud import bigquery

    out = []
    for name, v in values.items():
        if isinstance(v, date):
            out.append(bigquery.ScalarQueryParameter(name, "TIMESTAMP", datetime(v.year, v.month, v.day, tzinfo=timezone.utc)))
        else:
            out.append(bigquery.ArrayQueryParameter(name, "STRING", list(v)))
    return out


def dry_run_bytes(bq: EvalBigQuery, sql: str, params: list) -> int:
    """jobs.query with dryRun: true (free); returns totalBytesProcessed."""
    body = {
        "query": sql,
        "useLegacySql": False,
        "parameterMode": "NAMED",
        "queryParameters": [p.to_api_repr() for p in params],
        "dryRun": True,
        "location": LOCATION,
    }
    res = bq.session.post(f"{BQ_API}/projects/{bq.project}/queries", json=body, timeout=60)
    try:
        data = res.json() if res.content else {}
    except ValueError:
        data = {}
    if res.status_code != 200 or data.get("error"):
        message = (data.get("error") or {}).get("message") or f"HTTP {res.status_code}"
        raise BigQueryError(f"dry run failed: {message}")
    return int(data.get("totalBytesProcessed") or 0)


def run_entry(bq: EvalBigQuery, entry: dict, values: dict, project: str, *, dry_run: bool = False,
              max_bytes: int | None = None, use_cache: bool = True):
    """Execute (or dry-run) one BigQuery catalog entry. Returns (columns, rows, bytes processed).

    An executed job always carries maximumBytesBilled (the shared project's rule); a dry
    run bills nothing and needs none."""
    from google.cloud import bigquery

    sql = entry["sql"].replace(PLACEHOLDER, f"`{project}`")
    params = query_parameters(values)
    if dry_run:
        return [], [], dry_run_bytes(bq, sql, params)
    if not max_bytes:
        raise ValueError("every executed BigQuery job needs max_bytes (maximumBytesBilled)")
    cfg = bigquery.QueryJobConfig(
        query_parameters=params,
        use_query_cache=use_cache,
        labels={"app": "peoplelens", "purpose": "smoke"},
        maximum_bytes_billed=max_bytes,
    )
    result = bq.client.query_and_wait(sql, job_config=cfg, location=LOCATION, wait_timeout=60)
    columns = [f.name for f in result.schema]
    rows = [tuple(r.values()) for r in result]
    return columns, rows, result.total_bytes_processed


class BigQueryExecutor:
    """Text-to-SQL baseline executor in production: eval service account, 200 MiB cap."""

    kind = "bigquery"
    dialect = "bigquery"

    def __init__(self, project: str):
        from google.cloud import bigquery

        self.project = project
        self.bq = bq_client(project)
        self.cfg = bigquery.QueryJobConfig(
            default_dataset=f"{project}.peoplelens_semantic",
            maximum_bytes_billed=BASELINE_MAX_BYTES,
            labels={"app": "peoplelens", "purpose": "baseline"},
        )

    def run(self, sql: str):
        result = self.bq.client.query_and_wait(sql, job_config=self.cfg, location=LOCATION, wait_timeout=60)
        return [f.name for f in result.schema], [list(r.values()) for r in result]

    def close(self) -> None:
        self.bq.close()


def prod_baseline():
    from peoplelens.eval.llm_client import WorkersAiRestSql

    env = read_env_file(CLOUDFLARE_ENV)
    state = load_state()
    if not env.get("CLOUDFLARE_ACCOUNT_ID") or not env.get("CLOUDFLARE_API_TOKEN") or not state.get("gcp_project"):
        raise SystemExit("prod baseline needs .secrets/cloudflare.env and a bootstrapped project")
    return WorkersAiRestSql(env["CLOUDFLARE_ACCOUNT_ID"], env["CLOUDFLARE_API_TOKEN"], MODEL), BigQueryExecutor(state["gcp_project"])


def dumps(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n"
