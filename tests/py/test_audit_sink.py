"""Local audit trail: whitelisted metadata-only events in a pyiceberg SqlCatalog table."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from peoplelens.ops import bootstrap_audit, checks
from peoplelens.ops.checks import Plan, Result
from peoplelens.paths import GENERATED_DIR
from peoplelens.warehouse import audit_sink
from peoplelens.warehouse.audit_sink import FIELDS, AuditRecordError, AuditSink, clean_record
from peoplelens.warehouse.server import ThreadingHTTPServer, Warehouse, make_handler

EVENT = {"ts": "2026-10-10T00:00:00Z", "request_id": "r1", "role": "executive", "action": "answer",
         "catalog_key": "headcount|location|none", "rows": 5, "latency_ms": 12.5, "suppressed_primary": 0}


def test_whitelist_and_types():
    assert set(clean_record(EVENT)) == set(FIELDS)
    for bad in ({**EVENT, "question": "how many people in Legal"}, {**EVENT, "rows": "five"},
                {**EVENT, "role": "x" * 500}, {"role": "executive"}, [EVENT]):
        with pytest.raises(AuditRecordError):
            clean_record(bad)


def test_append_and_scan(tmp_path):
    sink = AuditSink(tmp_path / "ice")
    assert sink.append([EVENT, {**EVENT, "request_id": "r2", "ts": "2026-10-10T00:00:01Z", "action": "refusal"}]) == 2
    again = AuditSink(tmp_path / "ice")  # a new process sees the same Iceberg table
    rows = again.tail(10)
    assert [r["request_id"] for r in rows] == ["r1", "r2"]
    assert rows[0]["rows"] == 5 and rows[1]["action"] == "refusal"


def test_sidecar_audit_route(tmp_path, monkeypatch, require_warehouse):
    monkeypatch.setattr(audit_sink, "ICEBERG_DIR", tmp_path / "ice")
    monkeypatch.setattr(AuditSink.__init__, "__defaults__", (tmp_path / "ice",))
    wh = Warehouse(require_warehouse, GENERATED_DIR / "catalog.duckdb.json", "t" * 20)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(wh))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body, token="t" * 20):
        req = urllib.request.Request(base + "/v1/audit", data=json.dumps(body).encode(), method="POST",
                                     headers={"authorization": f"Bearer {token}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        assert post({"records": [EVENT]}) == (200, {"appended": 1})
        assert post({"records": [EVENT]}, token="wrong-token-wrong")[0] == 401
        status, body = post({"records": [{**EVENT, "question": "secret"}]})
        assert status == 400 and body["error"]["code"] == "BAD_AUDIT_RECORD"
        assert [r["request_id"] for r in AuditSink(tmp_path / "ice").tail()] == ["r1"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        wh.close()


class FakeApi:
    def __init__(self, queues=(), buckets=()):
        self.queues, self.buckets = list(queues), list(buckets)

    def result(self, method, path, body=None, **kw):
        if path == "/queues":
            return [{"queue_name": q} for q in self.queues]
        if path == "/r2/buckets":
            return {"buckets": [{"name": b} for b in self.buckets]}
        raise AssertionError(path)


class ListRunner(checks.Runner):
    def __init__(self, streams=(), sinks=(), pipelines=()):
        super().__init__()
        self.lists = {"streams": streams, "sinks": sinks, "pipelines": pipelines}

    def run(self, cmd, **kw):
        self.log.append(cmd)
        if cmd[-1] == "--json":
            kind = cmd[-3] if cmd[-3] in ("streams", "sinks") else "pipelines"
            items = [{"name": n, "id": f"id-{n}"} for n in self.lists[kind]]
            return Result(0, json.dumps(items))
        return Result(0, "")


def test_audit_bootstrap_lists_before_creating(tmp_path, monkeypatch):
    env = tmp_path / "cloudflare.env"
    env.write_text("CLOUDFLARE_ACCOUNT_ID=a\nCLOUDFLARE_API_TOKEN=t\nBASIN_CATALOG_TOKEN=c\n")
    monkeypatch.setattr(bootstrap_audit, "CLOUDFLARE_ENV", env)
    monkeypatch.setattr(bootstrap_audit, "GENERATED_CONFIG", tmp_path / "wrangler.audit.generated.jsonc")
    from peoplelens.ops import bootstrap_cloudflare

    monkeypatch.setattr(bootstrap_cloudflare, "DEPLOY_SECRETS", tmp_path / "deploy-secrets.json")
    monkeypatch.setattr(checks, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(bootstrap_audit, "load_state", checks.load_state)
    monkeypatch.setattr(bootstrap_audit, "save_state", checks.save_state)
    runner = ListRunner(streams=["peoplelens_audit"], sinks=["peoplelens_audit_sink"], pipelines=["peoplelens_audit_pipeline"])
    bootstrap_audit.phase_audit(FakeApi(queues=["peoplelens-audit"], buckets=["peoplelens-audit"]), runner, Plan())
    creates = [c for c in runner.log if "create" in c]
    assert creates == []  # everything existed
    cfg = json.loads((tmp_path / "wrangler.audit.generated.jsonc").read_text())
    prod = cfg["env"]["production"]
    assert prod["pipelines"] == [{"binding": "AUDIT_STREAM", "stream": "id-peoplelens_audit"}]
    assert prod["queues"]["producers"] == [{"binding": "AUDIT_QUEUE", "queue": "peoplelens-audit"}]
    deploy = runner.log[-1]
    assert deploy[:3] == ["npx", "wrangler", "deploy"] and "--secrets-file" in deploy
    assert deploy[deploy.index("--config") + 1] == "wrangler.audit.generated.jsonc"
    assert ["npm", "run", "build:web"] in runner.log
    assert not (tmp_path / "deploy-secrets.json").exists()
    state = checks.load_state()
    assert state["audit_hmac_key_set"] is True and state["audit_stream_id"] == "id-peoplelens_audit"
    # A later plain deploy keeps the audit bindings and does not mint a new HMAC key.
    later = ListRunner()
    bootstrap_cloudflare.deploy(later, Plan())
    assert later.log[-1][later.log[-1].index("--config") + 1] == "wrangler.audit.generated.jsonc"
    assert "--secrets-file" not in later.log[-1]
    fresh = ListRunner()
    with pytest.raises(bootstrap_audit.BootstrapError):  # stream id cannot be read back from an empty listing
        bootstrap_audit.phase_audit(FakeApi(), fresh, Plan())
    created = [c[:5] for c in fresh.log if "create" in c]
    assert ["npx", "wrangler", "queues", "create", "peoplelens-audit"] in created
