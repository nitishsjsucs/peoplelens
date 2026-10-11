"""Smoke step 6 (below the core cut line): the data-masking demo and the time-travel
recovery drill, against the production BigQuery project.

Masking demo: query `fct_workforce_monthly.base_salary_eom` (tagged `compensation`,
masked with DEFAULT_MASKING_VALUE) as the `peoplelens-analyst` service account,
which is a masked reader but not a fine-grained reader, and expect only masked
values (0 for INT64); then as the owner (a fine-grained reader) and expect real
salaries. Needs bootstrap phase C, including step 20 (token creator on the analyst
account for the owner, so gcloud can impersonate it).

Recovery drill: on a scratch copy of `peoplelens_core.dim_month` only (never a real
table), record a time, delete every row, restore with FOR SYSTEM_TIME AS OF into a
new table, check the row count, and always drop the scratch tables.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from peoplelens.ops.bootstrap_gcp import sa_email
from peoplelens.ops.checks import Runner

BQ = "https://bigquery.googleapis.com/bigquery/v2"
# Shared Google Cloud project (SPEC.md 25.4 E): every drill query is capped and labeled.
DRILL_MAX_BYTES = 100 * 1024 * 1024
LABELS = {"app": "peoplelens", "purpose": "drill"}


class DrillError(RuntimeError):
    pass


def _token(runner: Runner, impersonate: str | None = None) -> str:
    cmd = ["gcloud", "auth", "print-access-token"]
    if impersonate:
        cmd.append(f"--impersonate-service-account={impersonate}")
    res = runner.run(cmd)
    if not res.ok or not res.stdout.strip():
        raise DrillError(f"could not get an access token{' for ' + impersonate if impersonate else ''}: {res.stderr.strip()[:200]}")
    return res.stdout.strip()


def _query(project: str, token: str, sql: str, http=None) -> list[list]:
    body = {"query": sql, "useLegacySql": False, "location": "US", "timeoutMs": 30000,
            "maximumBytesBilled": str(DRILL_MAX_BYTES), "labels": dict(LABELS)}
    if http is not None:
        out = http(f"{BQ}/projects/{project}/queries", body, token)
    else:
        req = urllib.request.Request(f"{BQ}/projects/{project}/queries", data=json.dumps(body).encode(), method="POST")
        req.add_header("authorization", f"Bearer {token}")
        req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise DrillError(f"BigQuery HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    if out.get("error"):
        raise DrillError(str(out["error"].get("message"))[:300])
    return [[c.get("v") for c in r.get("f", [])] for r in out.get("rows", [])]


def masking_demo(project: str, runner: Runner, http=None) -> dict:
    sql = f"SELECT base_salary_eom FROM `{project}`.peoplelens_core.fct_workforce_monthly WHERE is_active_eom = 1 LIMIT 20"
    analyst = sa_email("peoplelens-analyst", project)
    masked_rows = _query(project, _token(runner, analyst), sql, http)
    owner_rows = _query(project, _token(runner), sql, http)
    masked_values = [r[0] for r in masked_rows]
    owner_values = [r[0] for r in owner_rows]
    masked_ok = bool(masked_values) and all(v in (None, "0", 0) for v in masked_values)
    owner_ok = bool(owner_values) and any(v not in (None, "0", 0) for v in owner_values)
    return {
        "ok": masked_ok and owner_ok,
        "analyst_sees_masked_values": masked_ok,
        "owner_sees_real_values": owner_ok,
        "rows_checked": len(masked_values),
    }


def time_travel_drill(project: str, runner: Runner, http=None, sleep=time.sleep, now_ms=lambda: int(time.time() * 1000)) -> dict:
    token = _token(runner)
    suffix = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    scratch = f"`{project}`.peoplelens_core.drill_dim_month_{suffix}"
    restored = f"`{project}`.peoplelens_core.drill_dim_month_{suffix}_restored"
    result = {"ok": False, "scratch_table": scratch.split(".")[-1]}
    try:
        _query(project, token, f"CREATE TABLE {scratch} AS SELECT * FROM `{project}`.peoplelens_core.dim_month", http)
        before = int(_query(project, token, f"SELECT COUNT(*) FROM {scratch}", http)[0][0])
        sleep(5)
        marker = now_ms()
        sleep(5)
        _query(project, token, f"DELETE FROM {scratch} WHERE TRUE", http)
        after_delete = int(_query(project, token, f"SELECT COUNT(*) FROM {scratch}", http)[0][0])
        _query(project, token,
               f"CREATE TABLE {restored} AS SELECT * FROM {scratch} FOR SYSTEM_TIME AS OF TIMESTAMP_MILLIS({marker})", http)
        recovered = int(_query(project, token, f"SELECT COUNT(*) FROM {restored}", http)[0][0])
        result.update(before=before, after_delete=after_delete, recovered=recovered,
                      ok=before > 0 and after_delete == 0 and recovered == before)
    finally:
        for table in (restored, scratch):
            try:
                _query(project, token, f"DROP TABLE IF EXISTS {table}", http)
            except DrillError:
                result["cleanup_failed"] = True
    return result
