"""Production smoke helpers: the read-only BigQuery path and the metadata-only gateway check.

Neither talks to Google or Cloudflare here; fakes stand in for the BigQuery client,
the authorized HTTP session and the Cloudflare transport.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from peoplelens.ops import prod_eval, smoke
from peoplelens.ops.cloudflare_api import API, CloudflareApi, Response

ENTRY = {"key": "headcount||none", "sql": "SELECT 1 FROM `peoplelens-project-id`.peoplelens_semantic.sem_workforce_monthly"}
VALUES = {"start_ts": date(2025, 12, 1), "end_ts": date(2025, 12, 31), "f_location": ["__all__"]}


class Field:
    def __init__(self, name):
        self.name = name


class Row:
    def __init__(self, *values):
        self._values = values

    def values(self):
        return self._values


class FakeRows(list):
    schema = [Field("headcount"), Field("cohort")]
    total_bytes_processed = 10485760


class FakeClient:
    def __init__(self):
        self.calls = []

    def query_and_wait(self, sql, **kw):
        self.calls.append(("query_and_wait", sql, kw))
        return FakeRows([Row(1780, 1780)])

    def query(self, *a, **kw):  # jobs.insert: not accepted with the bigquery.readonly scope
        raise AssertionError("Client.query uses jobs.insert, which the bigquery.readonly scope cannot call")


class FakeHttp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.payload = payload if payload is not None else {"totalBytesProcessed": "123456"}
        self.content = json.dumps(self.payload).encode()
        self.posts = []

    def post(self, url, json=None, timeout=None):  # noqa: A002 - requests' keyword
        self.posts.append((url, json))
        return self

    def json(self):
        return self.payload


def test_eval_credentials_are_read_only_and_queries_avoid_jobs_insert():
    assert prod_eval.READONLY_SCOPE == "https://www.googleapis.com/auth/bigquery.readonly"
    bq = prod_eval.EvalBigQuery("peoplelens-abc123", FakeClient(), FakeHttp())
    columns, rows, processed = prod_eval.run_entry(bq, ENTRY, VALUES, "peoplelens-abc123", max_bytes=1048576, use_cache=False)
    assert columns == ["headcount", "cohort"] and rows == [(1780, 1780)] and processed == 10485760
    name, sql, kw = bq.client.calls[0]
    assert "`peoplelens-abc123`.peoplelens_semantic" in sql and "peoplelens-project-id" not in sql
    cfg = kw["job_config"]
    assert cfg.maximum_bytes_billed == 1048576 and cfg.use_query_cache is False and kw["location"] == "US"
    assert {p.name for p in cfg.query_parameters} == set(VALUES)


def test_dry_run_is_a_jobs_query_call_with_typed_parameters():
    http = FakeHttp()
    bq = prod_eval.EvalBigQuery("peoplelens-abc123", FakeClient(), http)
    _, _, processed = prod_eval.run_entry(bq, ENTRY, VALUES, "peoplelens-abc123", dry_run=True)
    assert processed == 123456 and bq.client.calls == []
    url, body = http.posts[0]
    assert url == "https://bigquery.googleapis.com/bigquery/v2/projects/peoplelens-abc123/queries"
    assert body["dryRun"] is True and body["parameterMode"] == "NAMED" and body["useLegacySql"] is False
    types = {p["name"]: p["parameterType"]["type"] for p in body["queryParameters"]}
    assert types == {"start_ts": "TIMESTAMP", "end_ts": "TIMESTAMP", "f_location": "ARRAY"}
    bad = prod_eval.EvalBigQuery("p", FakeClient(), FakeHttp(400, {"error": {"message": "Unrecognized name: x"}}))
    with pytest.raises(prod_eval.BigQueryError, match="Unrecognized name"):
        prod_eval.run_entry(bad, ENTRY, VALUES, "peoplelens-abc123", dry_run=True)


@pytest.mark.parametrize(
    ("status", "body", "state"),
    [
        (404, {"success": False, "errors": [{"message": "not found"}]}, "absent"),
        (200, {}, "absent"),
        (200, {"success": True, "result": None, "errors": [], "messages": []}, "absent"),
        # A stored Workers AI request payload has "messages" but no "success": it is a body, not an envelope.
        (200, {"messages": [{"role": "user", "content": "What is the headcount in 2025?"}], "max_tokens": 300}, "present"),
        (200, {"messages": []}, "present"),
        # A stored Workers AI response payload is itself an envelope with a result.
        (200, {"success": True, "result": {"response": {"action": "query"}}, "errors": [], "messages": []}, "present"),
        (200, {"_raw": "data: {...}"}, "present"),
        (500, {"success": False}, "unknown"),
        (403, {"success": False}, "unknown"),
    ],
)
def test_payload_state(status, body, state):
    assert smoke.payload_state(status, body) == state


class GatewayTransport:
    def __init__(self, log, request=None, response=None, missing_first=0):
        self.log = log
        self.parts = {"request": request or Response(404, {}), "response": response or Response(404, {})}
        self.missing_first = missing_first
        self.calls = []

    def __call__(self, method, url, body, headers):
        path = url.replace(API, "")
        self.calls.append(path)
        for part, res in self.parts.items():
            if path.endswith(f"/logs/L1/{part}"):
                return res
        if path.endswith("/ai-gateway/gateways/peoplelens/logs/L1"):
            if self.missing_first:
                self.missing_first -= 1
                return Response(404, {"success": False, "errors": [{"message": "not found"}]})
            return Response(200, {"success": True, "result": self.log})
        return Response(404, {})


QUESTION = "What is the headcount in 2025?"
GOOD_LOG = {"id": "L1", "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "tokens_in": 900, "tokens_out": 60,
            "metadata": json.dumps({"request_id": "r1", "role": "executive", "turn": 1})}


def _gateway(transport, **kw):
    return smoke.step_gateway(CloudflareApi("acc", "tok", transport), [("L1", QUESTION)], sleep=lambda s: None, **kw)


def test_gateway_check_passes_only_for_a_metadata_only_log():
    out = _gateway(GatewayTransport(GOOD_LOG))
    assert out["ok"] and out["custom_metadata"] and out["payloads"] == {"request": "absent", "response": "absent"}


def test_gateway_check_fails_when_a_payload_is_stored():
    body = Response(200, {"messages": [{"role": "user", "content": QUESTION}]})
    out = _gateway(GatewayTransport(GOOD_LOG, request=body))
    assert not out["ok"] and out["payloads"]["request"] == "present"


def test_gateway_check_fails_on_body_heads_or_question_text_in_the_log():
    with_head = dict(GOOD_LOG, request_head='{"messages":[{"role":"system"')
    assert not _gateway(GatewayTransport(with_head))["ok"]
    with_text = dict(GOOD_LOG, metadata=json.dumps({"request_id": "r1", "role": "executive", "q": QUESTION}))
    out = _gateway(GatewayTransport(with_text))
    assert not out["ok"] and out["question_text_in_log"]


def test_gateway_check_needs_custom_metadata():
    out = _gateway(GatewayTransport(dict(GOOD_LOG, metadata=None)))
    assert not out["ok"] and not out["custom_metadata"]


def test_gateway_check_waits_for_the_log_to_be_written():
    t = GatewayTransport(GOOD_LOG, missing_first=3)
    assert _gateway(t)["ok"]
    never = GatewayTransport(GOOD_LOG, missing_first=99)
    out = _gateway(never, attempts=4)
    assert not out["ok"] and "not found after 4 attempts" in out["message"]


def test_gateway_check_reports_an_unreadable_payload_as_unknown():
    out = _gateway(GatewayTransport(GOOD_LOG, request=Response(500, {"success": False})))
    assert not out["ok"] and out["payloads"]["request"] == "unknown" and "dashboard" in out["message"]


def test_gateway_check_needs_a_log_id():
    assert smoke.step_gateway(CloudflareApi("acc", "tok", GatewayTransport(GOOD_LOG)), [])["ok"] is False


class Reply:
    def __init__(self, status, body):
        self.status, self.body = status, body


class FakeWorker:
    """Answers like a healthy production Worker; the HRBP token is rejected by Access."""

    def __init__(self, base_url, headers):
        self.secret = headers["CF-Access-Client-Secret"]

    def session(self):
        if self.secret == "bad":
            raise RuntimeError("could not create a session: HTTP 302 {}")
        return "s1"

    def ask(self, session, message):
        if "gender" in message:
            return Reply(200, {"type": "refusal", "refusal": {"code": "DIMENSION_NOT_APPROVED"}})
        if "by department" in message:
            return Reply(200, {"type": "answer", "answer": {"rows": [{"department": "Legal", "suppressed": True}]}})
        if "excluding Legal" in message:
            return Reply(200, {"type": "answer", "answer": {"rows": [], "suppressed_cells": {"complement": 1}}})
        return Reply(200, {"type": "answer", "meta": {"gateway_log_id": "L1"}})


def test_service_token_step_reports_a_rejected_role_and_keeps_going(tmp_path, monkeypatch):
    env = tmp_path / "prod_eval.env"
    keys = ["PEOPLE_ANALYTICS", "HR_BUSINESS_PARTNER", "TALENT_ACQUISITION", "EXECUTIVE"]
    env.write_text("".join(f"{k}_CLIENT_ID={k.lower()}.access\n{k}_CLIENT_SECRET={'bad' if k == 'HR_BUSINESS_PARTNER' else 's'}\n" for k in keys))
    monkeypatch.setattr(smoke, "PROD_EVAL_ENV", env)
    out = smoke.step_service_tokens("https://peoplelens.example.workers.dev", client_factory=FakeWorker)
    assert out["ok"] is False
    assert out["roles"]["hr_business_partner"]["ok"] is False and "302" in out["roles"]["hr_business_partner"]["error"]
    assert all(out["roles"][r]["ok"] for r in ("people_analytics", "talent_acquisition", "executive"))
    assert out["roles"]["executive"]["legal_suppressed"] and out["roles"]["executive"]["complement_suppressed"]
    assert out["gateway_logs"][0] == ("L1", "What is the headcount in 2025?")


def test_an_executed_entry_always_carries_a_byte_cap():
    bq = prod_eval.EvalBigQuery("peoplelens-abc123", FakeClient(), FakeHttp())
    with pytest.raises(ValueError, match="max_bytes"):
        prod_eval.run_entry(bq, ENTRY, VALUES, "peoplelens-abc123")
    assert bq.client.calls == []


def test_parity_runs_every_entry_with_the_200_mib_cap(monkeypatch):
    seen = []

    def fake_run_entry(client, entry, values, project, **kw):
        seen.append(kw)
        return [], [], 0

    monkeypatch.setattr(smoke, "run_entry", fake_run_entry)
    monkeypatch.setattr(smoke, "compare", lambda *a, **k: [])
    out = smoke.step_parity(object(), "peoplelens-abc123")
    assert out["ok"] and len(seen) == 312
    assert all(kw == {"max_bytes": 200 * 1024 * 1024} for kw in seen)
    assert prod_eval.PARITY_MAX_BYTES == 200 * 1024 * 1024


class MeClient:
    def __init__(self, status=200, budget=None):
        self.status, self.budget = status, budget

    def __call__(self, base_url, headers):
        return self

    def me(self):
        from peoplelens.dev.client import Reply

        body = {"role": "executive"}
        if self.budget is not None:
            body["ai_budget"] = self.budget
        return Reply(self.status, body, 1.0)


def test_neuron_preflight_needs_4800_left_today():
    assert smoke.SMOKE_PLANNER_CALLS * 185.6 <= smoke.SMOKE_MIN_NEURONS <= 6000  # upper bound per planner call, SPEC.md 25.5
    ok = smoke.step_neuron_preflight("https://w", MeClient(budget={"day": "2026-10-11", "used": 1200, "cap": 6000}))
    assert ok["ok"] and ok["left"] == 4800
    short = smoke.step_neuron_preflight("https://w", MeClient(budget={"day": "2026-10-11", "used": 1201, "cap": 6000}))
    assert not short["ok"] and "00:00 UTC" in short["message"]
    stale = smoke.step_neuron_preflight("https://w", MeClient())
    assert not stale["ok"] and "make deploy" in stale["message"]


def test_tail_outcomes_reads_single_line_and_pretty_printed_events():
    text = ('{"outcome": "exception", "scriptName": "peoplelens", "logs": []}\n'
            + json.dumps({"outcome": "exceededCpu", "scriptName": "peoplelens", "event": {"request": {"url": "https://x/api/ask"}}}, indent=2)
            + "\nnot json\n{\"outcome\": \"ok\"}")
    assert smoke.tail_outcomes(text) == ["exception", "exceededCpu", "ok"]
    assert smoke.tail_outcomes("") == []


def test_cpu_step_runs_wrangler_tail_and_fails_on_exceeded_cpu():
    class FakeProc:
        pid = 999_999_999

        def __init__(self, out):
            self.out = out

        def terminate(self):
            pass

        def communicate(self, timeout=None):
            return self.out, ""

        def kill(self):
            pass

    launched = {}

    def popen(cmd, **kw):
        launched["cmd"], launched["kw"] = cmd, kw
        return FakeProc('{"outcome": "exceededCpu"}\n{"outcome": "exception"}\n')

    tail = smoke.CpuTail(popen=popen, settle_s=0)
    tail.start()
    out = tail.stop()
    assert launched["cmd"] == ["npx", "wrangler", "tail", "peoplelens", "--env", "production", "--format", "json", "--status", "error"]
    assert "CLOUDFLARE_API_TOKEN" not in launched["kw"]["env"] and launched["kw"]["start_new_session"] is True
    assert out["ok"] is False and out["exceeded_cpu"] == 1 and out["error_events"] == 2
    clean = smoke.CpuTail(popen=lambda cmd, **kw: FakeProc(""), settle_s=0)
    clean.start()
    assert clean.stop() == {"ok": True, "error_events": 0, "exceeded_cpu": 0, "outcomes": [], "method": " ".join(smoke.TAIL_CMD)}


def test_service_token_step_asks_27_questions_and_collects_their_request_ids(monkeypatch):
    from peoplelens.dev.client import Reply

    class Client:
        n = 0

        def __init__(self, base_url, headers):
            pass

        def session(self):
            return "s"

        def ask(self, session, text):
            Client.n += 1
            if "gender" in text:
                return Reply(200, {"request_id": f"r{Client.n}", "type": "refusal", "refusal": {"code": "DIMENSION_NOT_APPROVED"}}, 1)
            rows = [{"department": "Legal", "suppressed": True}]
            return Reply(200, {"request_id": f"r{Client.n}", "type": "answer",
                               "answer": {"rows": rows, "suppressed_cells": {"complement": 1}}, "meta": {}}, 1)

    ids: list[str] = []
    out = smoke.step_service_tokens("https://w", client_factory=Client, request_ids=ids)
    assert out["ok"] is True
    assert len(ids) == smoke.SMOKE_QUESTIONS == 27 and len(set(ids)) == 27


def test_audit_step_checks_each_step_4_request_exactly_once(tmp_path):
    from peoplelens.warehouse.audit_commit import local_store
    from peoplelens.warehouse.audit_sink import FIELDS, local_catalog

    store, catalog = local_store(tmp_path / "staging"), local_catalog(tmp_path / "ice")
    ids = [f"0b9c1f9e-7a3b-4f00-9c1e-{n:012d}" for n in range(27)]

    def ev(rid):
        return {**{f: None for f in FIELDS}, "ts": "2026-10-11T12:00:00.000Z", "request_id": rid, "role": "executive", "action": "answer"}

    store.put("staging/v1/dt=2026-10-11/" + "c" * 32 + ".ndjson", "".join(json.dumps(ev(i)) + "\n" for i in ids[:20]).encode())
    for rid in ids[18:]:  # two of them also put directly, as after a failed send
        store.put(f"staging/v1/dt=2026-10-11/direct-{rid}.ndjson", (json.dumps(ev(rid)) + "\n").encode())
    out = smoke.step_audit(ids, wait_s=0, sleep=lambda s: None, store=store, catalog=catalog)
    # The second commit replays all 10 committed objects (1 batch, 9 direct): 27 events found, none appended.
    assert out == {"ok": True, "asks": 27, "appended_first": 27, "replayed_objects": 10, "appended_second": 0,
                   "already_in_table_second": 27, "missing": [], "repeated": []}
    missing = smoke.step_audit(ids + ["never-staged"], wait_s=0, sleep=lambda s: None, store=store, catalog=catalog)
    assert missing["ok"] is False and missing["missing"] == ["never-staged"]


def test_the_smoke_record_never_carries_the_project_account_subdomain_or_an_email():
    project, account = "shared-proj-4711", "0123456789abcdef0123456789abcdef"
    record = {
        "worker_url": "https://peoplelens.someone-personal.workers.dev",
        "dry_run": {"ok": False, "errors": [f"headcount||none: 403 POST https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries"]},
        "masking_demo": {"ok": False, "message": f"impersonating peoplelens-analyst@{project}.iam.gserviceaccount.com failed for owner@gmail.com"},
        "gateway": {"ok": False, "message": f"GET /accounts/{account}/ai-gateway/gateways/peoplelens/logs failed"},
        "nested": [{"n": 3, "s": f"{project}:{project}"}],
    }
    out = smoke.scrub(record, {project: "<shared-project-id>", account: "<account-id>",
                               smoke._subdomain(record["worker_url"]): "<subdomain>"})
    text = json.dumps(out)
    for secret in (project, account, "someone-personal", "owner@gmail.com"):
        assert secret not in text
    assert out["worker_url"] == "https://peoplelens.<subdomain>.workers.dev"
    assert "peoplelens-analyst@<shared-project-id>.iam.gserviceaccount.com" in out["masking_demo"]["message"]
    assert "<email>" in out["masking_demo"]["message"] and out["nested"][0]["n"] == 3
    assert smoke._subdomain("https://example.com") == ""


def test_cost_limit_step_records_only_the_expected_refusal(monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("400 POST https://bigquery.googleapis.com/bigquery/v2/projects/shared-proj-4711/queries: "
                           "Query exceeded limit for bytes billed: 1048576. 10485760 or higher required.")

    monkeypatch.setattr(smoke, "run_entry", refuse)
    assert smoke.step_cost_limit(None, "shared-proj-4711") == {"ok": True, "message": smoke.BYTES_BILLED_MESSAGE}
    monkeypatch.setattr(smoke, "run_entry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403 Access Denied")))
    assert smoke.step_cost_limit(None, "p") == {"ok": False, "message": "403 Access Denied"}
