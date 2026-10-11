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
