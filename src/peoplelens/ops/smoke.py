"""`pl-ops smoke`: production checks after bootstrap phases A and B (SPEC.md 17.8).

1. Dry-run all 312 BigQuery catalog entries (free).
2. --parity (default on): execute all 312 entries once each as peoplelens-eval and
   compare with the reference implementation (the check behind "MetricFlow over BigQuery").
3. Cost limit: one entry with maximumBytesBilled = 1 MiB and no cache must be refused by BigQuery.
4. As each of the four service tokens: one question per permitted metric, Legal
   suppressed, a refusal for an unapproved dimension, complement suppression for
   "headcount excluding Legal".
5. AI Gateway: a returned gateway_log_id resolves to a log carrying our custom
   metadata, with no stored request or response payload and the question text
   nowhere in the log (metadata-only logging). Logs are written asynchronously,
   so the log is polled for up to a minute.
6. --drills: masking demo and time-travel recovery drill (ops/drills.py).
A JSON record is written to eval/results/<date>-prod-smoke.json.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

from peoplelens.catalog.parity import bound_values, compare, param_sets
from peoplelens.dev.client import WorkerClient
from peoplelens.ops.bootstrap_access import ROLE_KEYS
from peoplelens.ops.checks import CLOUDFLARE_ENV, PROD_EVAL_ENV, load_state, read_env_file
from peoplelens.ops.cloudflare_api import CloudflareApi, CloudflareError
from peoplelens.ops.prod_eval import bq_client, dumps, run_entry
from peoplelens.paths import EVAL_DIR, GENERATED_DIR
from peoplelens.policy import load

BYTES_BILLED_MESSAGE = "Query exceeded limit for bytes billed"


def _catalog() -> dict:
    return json.loads((GENERATED_DIR / "catalog.bigquery.json").read_text())


def step_dry_run(client, project: str) -> dict:
    policy = load()
    errors, total = [], 0
    for i, entry in enumerate(sorted(_catalog()["entries"].values(), key=lambda e: e["key"])):
        try:
            _, _, b = run_entry(client, entry, bound_values(policy, entry, param_sets(policy, entry, i)[0]), project, dry_run=True)
            total += b or 0
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{entry['key']}: {str(exc)[:160]}")
    return {"entries": 312, "errors": errors, "bytes_processed_total": total, "ok": not errors}


def step_parity(client, project: str) -> dict:
    policy = load()
    problems, processed = [], 0
    for i, entry in enumerate(sorted(_catalog()["entries"].values(), key=lambda e: e["key"])):
        ps = param_sets(policy, entry, i)[0]
        try:
            columns, rows, b = run_entry(client, entry, bound_values(policy, entry, ps), project)
            processed += b or 0
            problems += compare(entry, columns, rows, ps, policy)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{entry['key']}: {str(exc)[:160]}")
    return {"entries": 312, "problems": problems[:50], "problem_count": len(problems), "bytes_processed_total": processed, "ok": not problems}


def step_cost_limit(client, project: str) -> dict:
    policy = load()
    entry = _catalog()["entries"]["headcount||none"]
    try:
        run_entry(client, entry, bound_values(policy, entry, param_sets(policy, entry, 0)[0]), project,
                  max_bytes=1048576, use_cache=False)
    except Exception as exc:  # noqa: BLE001
        refused = BYTES_BILLED_MESSAGE in str(exc)
        return {"ok": refused, "message": str(exc)[:200]}
    return {"ok": False, "message": "BigQuery ran the query despite maximumBytesBilled = 1 MiB"}


def step_service_tokens(base_url: str) -> dict:
    policy = load()
    env = read_env_file(PROD_EVAL_ENV)
    out = {"ok": True, "roles": {}, "gateway_logs": []}
    for role, key in ROLE_KEYS.items():
        client = WorkerClient(base_url, {"CF-Access-Client-Id": env[f"{key}_CLIENT_ID"], "CF-Access-Client-Secret": env[f"{key}_CLIENT_SECRET"]})
        results = {}
        session = client.session()
        for metric in policy.roles[role]["metrics"]:
            question = f"What is the {policy.metrics[metric]['label'].lower()} in 2025?"
            r = client.ask(session, question)
            results[metric] = r.body.get("type")
            if r.body.get("meta", {}).get("gateway_log_id"):
                out["gateway_logs"].append((r.body["meta"]["gateway_log_id"], question))
        gender = client.ask(client.session(), "headcount by gender")
        results["unapproved_refused"] = (gender.body.get("refusal") or {}).get("code") == "DIMENSION_NOT_APPROVED"
        if role in ("people_analytics", "executive"):
            r = client.ask(client.session(), "headcount by department")
            rows = (r.body.get("answer") or {}).get("rows", [])
            results["legal_suppressed"] = any(x.get("department") == "Legal" and x.get("suppressed") for x in rows)
            s = client.session()
            r = client.ask(s, "headcount right now excluding Legal")
            cells = (r.body.get("answer") or {}).get("suppressed_cells", {})
            results["complement_suppressed"] = cells.get("complement", 0) >= 1
        ok = all(v == "answer" for k, v in results.items() if k in policy.metrics) and all(
            v is True for k, v in results.items() if k not in policy.metrics
        )
        out["roles"][role] = {"ok": ok, **results}
        out["ok"] = out["ok"] and ok
    return out


ENVELOPE_KEYS = {"success", "errors", "messages", "result", "result_info"}
EMPTY = (None, "", {}, [])


def payload_state(status: int, body) -> str:
    """'absent', 'present' or 'unknown' for GET .../logs/{id}/request or /response.

    Those endpoints return the stored payload itself. A Workers AI request payload
    ({"messages": ...}) has no "success" key, so it is never mistaken for an empty
    API envelope; a Workers AI response payload is itself an envelope with a non-empty
    result. Any status other than 2xx or 404 is reported as unknown (not as absent).
    """
    if status == 404:
        return "absent"
    if not 200 <= status < 300:
        return "unknown"
    if body in EMPTY:
        return "absent"
    if isinstance(body, dict) and "success" in body and set(body) <= ENVELOPE_KEYS:
        return "absent" if body.get("result") in EMPTY else "present"
    return "present"


def step_gateway(api: CloudflareApi, logs: list[tuple[str, str]], attempts: int = 12, wait_s: float = 5.0,
                 sleep=time.sleep) -> dict:
    """Metadata-only logging, checked on a real gateway log: custom metadata present;
    no request or response payload stored; the question text appears nowhere in the log."""
    if not logs:
        return {"ok": False, "message": "no gateway_log_id returned by the Worker"}
    log_id, question = logs[0]
    method = "GET /ai-gateway/gateways/peoplelens/logs/{id} plus /request and /response"
    try:
        log = None
        for i in range(attempts):  # logs are written asynchronously
            log = api.gateway_log(log_id)
            if log:
                break
            if i < attempts - 1:
                sleep(wait_s)
        if not log:
            return {"ok": False, "log_id": log_id, "method": method,
                    "message": f"log {log_id} not found after {attempts} attempts; check AI > AI Gateway > peoplelens > Logs"}
        meta = log.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        has_meta = isinstance(meta, dict) and "request_id" in meta and "role" in meta
        heads = {f: log.get(f) not in EMPTY for f in ("request_head", "response_head")}
        question_in_log = question.lower() in json.dumps(log).lower()
        payloads = {}
        for part in ("request", "response"):
            res = api.gateway_log_part(log_id, part)
            payloads[part] = payload_state(res.status, res.body)
    except CloudflareError as exc:
        return {"ok": False, "message": str(exc)[:200], "method": method}
    unknown = [p for p, v in payloads.items() if v == "unknown"]
    ok = has_meta and not any(heads.values()) and not question_in_log and all(v == "absent" for v in payloads.values())
    out = {"ok": ok, "log_id": log_id, "custom_metadata": has_meta, "heads_present": heads,
           "question_text_in_log": question_in_log, "payloads": payloads, "method": method}
    if unknown:
        out["message"] = (f"the API did not say whether the {', '.join(unknown)} payload is stored; "
                          "open this log in the dashboard (AI > AI Gateway > peoplelens > Logs) and check")
    return out


def run(parity: bool = True, drills: bool = False) -> int:
    state = load_state()
    project = state.get("gcp_project")
    if not project or not state.get("worker_url"):
        print("smoke needs bootstrap phases A and B (.secrets/bootstrap-state.json)", file=sys.stderr)
        return 2
    env = read_env_file(CLOUDFLARE_ENV)
    tokens = read_env_file(PROD_EVAL_ENV)
    if not env.get("CLOUDFLARE_API_TOKEN") or not all(tokens.get(f"{k}_CLIENT_SECRET") for k in ROLE_KEYS.values()):
        print("smoke needs .secrets/cloudflare.env and the four service tokens in .secrets/prod_eval.env (bootstrap phase B)", file=sys.stderr)
        return 2
    api = CloudflareApi(env["CLOUDFLARE_ACCOUNT_ID"], env["CLOUDFLARE_API_TOKEN"])
    client = bq_client(project)
    record: dict = {"date": date.today().isoformat(), "project_is_set": True, "worker_url": state["worker_url"]}
    record["dry_run"] = step_dry_run(client, project)
    print(f"1. dry runs: {'ok' if record['dry_run']['ok'] else 'FAILED'}")
    if parity:
        record["parity"] = step_parity(client, project)
        print(f"2. parity (312 entries vs reference): {'ok' if record['parity']['ok'] else 'FAILED'}")
    record["cost_limit"] = step_cost_limit(client, project)
    print(f"3. BigQuery refuses a 1 MiB maximumBytesBilled job: {'ok' if record['cost_limit']['ok'] else 'FAILED'}")
    record["service_tokens"] = step_service_tokens(state["worker_url"])
    print(f"4. four roles via service tokens: {'ok' if record['service_tokens']['ok'] else 'FAILED'}")
    record["gateway"] = step_gateway(api, record["service_tokens"].get("gateway_logs", []))
    print(f"5. AI Gateway metadata-only log: {'ok' if record['gateway']['ok'] else 'FAILED'}")
    record["service_tokens"].pop("gateway_logs", None)
    if drills:
        from peoplelens.ops.checks import Runner
        from peoplelens.ops.drills import DrillError, masking_demo, time_travel_drill

        runner = Runner()
        for name, fn in (("masking_demo", masking_demo), ("time_travel_drill", time_travel_drill)):
            try:
                record[name] = fn(project, runner)
            except DrillError as exc:
                record[name] = {"ok": False, "message": str(exc)}
            print(f"6. {name.replace('_', ' ')}: {'ok' if record[name]['ok'] else 'FAILED'}")
    client.close()
    for key in ("dry_run", "parity", "cost_limit", "service_tokens", "gateway", "masking_demo", "time_travel_drill"):
        step = record.get(key)
        if isinstance(step, dict) and not step.get("ok"):
            detail = step.get("message") or step.get("errors") or step.get("problems") or step.get("roles") or step
            print(f"   {key}: {str(detail)[:400]}")
    path = EVAL_DIR / "results" / f"{record['date']}-prod-smoke.json"
    Path(path).write_text(dumps(record))
    print(f"wrote {path}")
    steps = [record[k]["ok"] for k in ("dry_run", "cost_limit", "service_tokens", "gateway")] + ([record["parity"]["ok"]] if parity else [])
    return 0 if all(steps) else 1
