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
7. Workers Free CPU: `wrangler tail` runs during step 4 and the step fails on any
   event whose outcome is exceededCpu (Error 1102).
8. --with-audit: staged audit events are committed into Basin Catalog, and each of
   step 4's request ids is in the table exactly once, also after a second commit.
Before step 4 smoke reads /api/me and stops if fewer than 4,800 of this repo's daily
Workers AI neurons are left (step 4 makes 23 planner calls, at most 4,268 neurons).
A JSON record is written to eval/results/<date>-prod-smoke.json.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from peoplelens.catalog.parity import bound_values, compare, param_sets
from peoplelens.dev.client import WorkerClient
from peoplelens.ops.bootstrap_access import ROLE_KEYS
from peoplelens.ops.checks import CLOUDFLARE_ENV, PROD_EVAL_ENV, load_state, read_env_file
from peoplelens.ops.cloudflare_api import CloudflareApi, CloudflareError
from peoplelens.ops.prod_eval import PARITY_MAX_BYTES, bq_client, dumps, run_entry
from peoplelens.paths import EVAL_DIR, GENERATED_DIR, WORKER_DIR
from peoplelens.policy import load

BYTES_BILLED_MESSAGE = "Query exceeded limit for bytes billed"
# Step 4: 27 questions, 23 of them reach the planner (4 are screened). At the planner's
# upper bound of 185.6 neurons per call that is at most 4,268; smoke needs 4,800 left.
SMOKE_QUESTIONS = 27
SMOKE_PLANNER_CALLS = 23
SMOKE_MIN_NEURONS = 4800
TAIL_CMD = ["npx", "wrangler", "tail", "peoplelens", "--env", "production", "--format", "json", "--status", "error"]


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
            columns, rows, b = run_entry(client, entry, bound_values(policy, entry, ps), project, max_bytes=PARITY_MAX_BYTES)
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


def _role_checks(client: WorkerClient, role: str, policy, logs: list, request_ids: list | None = None) -> dict:
    results: dict = {}
    ids = request_ids if request_ids is not None else []

    def ask(session, question):
        r = client.ask(session, question)
        if isinstance(r.body, dict) and r.body.get("request_id"):
            ids.append(r.body["request_id"])
        return r

    session = client.session()
    for metric in policy.roles[role]["metrics"]:
        question = f"What is the {policy.metrics[metric]['label'].lower()} in 2025?"
        r = ask(session, question)
        results[metric] = r.body.get("type")
        if r.body.get("meta", {}).get("gateway_log_id"):
            logs.append((r.body["meta"]["gateway_log_id"], question))
    gender = ask(client.session(), "headcount by gender")
    results["unapproved_refused"] = (gender.body.get("refusal") or {}).get("code") == "DIMENSION_NOT_APPROVED"
    if role in ("people_analytics", "executive"):
        r = ask(client.session(), "headcount by department")
        rows = (r.body.get("answer") or {}).get("rows", [])
        results["legal_suppressed"] = any(x.get("department") == "Legal" and x.get("suppressed") for x in rows)
        r = ask(client.session(), "headcount right now excluding Legal")
        cells = (r.body.get("answer") or {}).get("suppressed_cells", {})
        results["complement_suppressed"] = cells.get("complement", 0) >= 1
    return results


def _svc_headers(role: str) -> dict[str, str]:
    env = read_env_file(PROD_EVAL_ENV)
    key = ROLE_KEYS[role]
    return {"CF-Access-Client-Id": env.get(f"{key}_CLIENT_ID", ""), "CF-Access-Client-Secret": env.get(f"{key}_CLIENT_SECRET", "")}


def step_neuron_preflight(base_url: str, client_factory=WorkerClient) -> dict:
    """Step 4 makes 23 planner calls; refuse to start it with fewer than 4,800 neurons left today."""
    r = client_factory(base_url, _svc_headers("executive")).me()
    budget = (r.body or {}).get("ai_budget") if r.status == 200 and isinstance(r.body, dict) else None
    if not isinstance(budget, dict) or not isinstance(budget.get("cap"), int):
        return {"ok": False, "message": f"/api/me answered HTTP {r.status} without ai_budget; deploy the current commit (make deploy)"}
    left = budget["cap"] - (budget.get("used") or 0)
    out = {"ok": left >= SMOKE_MIN_NEURONS, "day": budget.get("day"), "used": budget.get("used"), "cap": budget["cap"],
           "left": left, "needed": SMOKE_MIN_NEURONS}
    if not out["ok"]:
        out["message"] = (f"only {left} of this repo's {budget['cap']} daily Workers AI neurons are left and step 4 needs "
                          f"{SMOKE_MIN_NEURONS}; run smoke after 00:00 UTC on a day without a production eval")
    return out


def tail_outcomes(text: str) -> list[str]:
    """`outcome` of every JSON event in `wrangler tail --format json` output (one object per
    event, on one line or pretty-printed)."""
    decoder = json.JSONDecoder()
    outcomes, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i] not in "{":
            i += 1
        if i >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict) and "outcome" in obj:
            outcomes.append(str(obj["outcome"]))
        i = end
    return outcomes


class CpuTail:
    """Step 7: `wrangler tail` (errors only: exception, exceededCpu, exceededMemory, unknown,
    per wrangler 4.149.0's source) while step 4 runs; any exceededCpu outcome fails the step."""

    def __init__(self, popen=subprocess.Popen, settle_s: float = 5.0, sleep=time.sleep):
        self.popen, self.settle_s, self.sleep = popen, settle_s, sleep
        self.proc = None

    def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("CLOUDFLARE_API_TOKEN",)}
        account = read_env_file(CLOUDFLARE_ENV).get("CLOUDFLARE_ACCOUNT_ID")
        if account:
            env["CLOUDFLARE_ACCOUNT_ID"] = account
        env["WRANGLER_SEND_METRICS"] = "false"
        self.proc = self.popen(TAIL_CMD, cwd=str(WORKER_DIR), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, start_new_session=True)

    def stop(self) -> dict:
        if self.proc is None:
            return {"ok": False, "message": "wrangler tail was not started"}
        self.sleep(self.settle_s)  # events of the last requests arrive a moment later
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError):
            self.proc.terminate()
        try:
            out, _ = self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate()
        outcomes = tail_outcomes(out or "")
        exceeded = outcomes.count("exceededCpu")
        result = {"ok": exceeded == 0, "error_events": len(outcomes), "exceeded_cpu": exceeded,
                  "outcomes": sorted(set(outcomes)), "method": " ".join(TAIL_CMD)}
        if exceeded:
            result["message"] = f"{exceeded} requests exceeded the Workers Free CPU limit (Error 1102); see SPEC.md risk 20"
        return result


def step_audit(request_ids: list[str], wait_s: float = 120.0, sleep=time.sleep, commit_fn=None, store=None, catalog=None) -> dict:
    """Step 8: each of step 4's request ids reaches the Iceberg table exactly once, also
    after a replay: every object the first commit consumed is staged again and committed
    a second time, which must append nothing."""
    from peoplelens.ops.cli import audit_target
    from peoplelens.warehouse.audit_commit import STAGING_PREFIX, commit, staged_ids, valid_key
    from peoplelens.warehouse.audit_sink import load_or_create_table

    if store is None or catalog is None:
        target = audit_target()
        if isinstance(target, str):
            return {"ok": False, "message": target}
        store, catalog = target
    commit_fn = commit_fn or commit
    wanted = set(request_ids)
    deadline = time.monotonic() + wait_s
    while not wanted <= staged_ids(store) and time.monotonic() < deadline:  # the consumer batches up to 30 s
        sleep(5.0)
    kept = {k: store.read(k) for k in store.list(STAGING_PREFIX) if valid_key(k)}
    first = commit_fn(store, catalog)
    for key, data in kept.items():  # redeliver what was just committed
        store.put(key, data)
    second = commit_fn(store, catalog)
    ids = load_or_create_table(catalog).scan(selected_fields=("request_id",)).to_arrow().column("request_id").to_pylist()
    counts = {i: ids.count(i) for i in wanted}
    missing = sorted(i for i, c in counts.items() if c == 0)
    repeated = sorted(i for i, c in counts.items() if c > 1)
    return {"ok": not missing and not repeated and len(wanted) == SMOKE_QUESTIONS and second.appended == 0,
            "asks": len(wanted), "appended_first": first.appended, "replayed_objects": len(kept),
            "appended_second": second.appended, "already_in_table_second": second.already_in_table,
            "missing": missing[:5], "repeated": repeated[:5]}


def step_service_tokens(base_url: str, client_factory=WorkerClient, request_ids: list | None = None) -> dict:
    policy = load()
    out = {"ok": True, "roles": {}, "gateway_logs": []}
    for role in ROLE_KEYS:
        client = client_factory(base_url, _svc_headers(role))
        try:
            results = _role_checks(client, role, policy, out["gateway_logs"], request_ids)
        except Exception as exc:  # noqa: BLE001 - report the role (for example an Access redirect) and go on
            out["roles"][role] = {"ok": False, "error": str(exc)[:200]}
            out["ok"] = False
            continue
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


def run(parity: bool = True, drills: bool = False, with_audit: bool = False) -> int:
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
    record["neuron_preflight"] = step_neuron_preflight(state["worker_url"])
    print(f"4a. Workers AI share left today: {record['neuron_preflight'].get('left')} (needs {SMOKE_MIN_NEURONS}): "
          f"{'ok' if record['neuron_preflight']['ok'] else 'STOPPED'}")
    if record["neuron_preflight"]["ok"]:
        request_ids: list[str] = []
        tail = CpuTail()
        tail.start()
        try:
            record["service_tokens"] = step_service_tokens(state["worker_url"], request_ids=request_ids)
        finally:
            record["cpu"] = tail.stop()
        print(f"4. four roles via service tokens: {'ok' if record['service_tokens']['ok'] else 'FAILED'}")
        record["gateway"] = step_gateway(api, record["service_tokens"].get("gateway_logs", []))
        print(f"5. AI Gateway metadata-only log: {'ok' if record['gateway']['ok'] else 'FAILED'}")
        record["service_tokens"].pop("gateway_logs", None)
        print(f"7. no request exceeded the Workers Free CPU limit: {'ok' if record['cpu']['ok'] else 'FAILED'} "
              f"({record['cpu'].get('error_events', 0)} error events)")
        if with_audit:
            record["audit"] = step_audit(request_ids)
            print(f"8. audit: each step 4 request in the Iceberg table exactly once: {'ok' if record['audit']['ok'] else 'FAILED'}")
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
    for key in ("dry_run", "parity", "cost_limit", "neuron_preflight", "service_tokens", "gateway", "cpu", "audit",
                "masking_demo", "time_travel_drill"):
        step = record.get(key)
        if isinstance(step, dict) and not step.get("ok"):
            detail = step.get("message") or step.get("errors") or step.get("problems") or step.get("roles") or step
            print(f"   {key}: {str(detail)[:400]}")
    path = EVAL_DIR / "results" / f"{record['date']}-prod-smoke.json"
    Path(path).write_text(dumps(record))
    print(f"wrote {path}")
    required = ["dry_run", "cost_limit", "neuron_preflight", "service_tokens", "gateway", "cpu"] + (["parity"] if parity else []) + (
        ["audit"] if with_audit else [])
    steps = [bool(record.get(k, {}).get("ok")) for k in required]
    return 0 if all(steps) else 1
