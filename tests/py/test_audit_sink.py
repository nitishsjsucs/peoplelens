"""Audit trail plumbing: whitelist, field parity with the Worker, the sidecar staging
route, and the production bootstrap without Basin Pipelines (SPEC.md 25.4 A)."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from peoplelens.ops import bootstrap_audit, bootstrap_cloudflare, checks
from peoplelens.ops.checks import Plan, Result
from peoplelens.paths import GENERATED_DIR, WORKER_DIR
from peoplelens.warehouse.audit_sink import FIELDS, AuditRecordError, AuditSink, clean_record
from peoplelens.warehouse.server import ThreadingHTTPServer, Warehouse, make_handler

EVENT = {"ts": "2026-10-10T00:00:00Z", "request_id": "r1", "role": "executive", "action": "answer",
         "catalog_key": "headcount|location|none", "rows": 5, "latency_ms": 12.5, "suppressed_primary": 0}
BATCH_KEY = "staging/v1/dt=2026-10-10/" + "a" * 32 + ".ndjson"
DIRECT_KEY = "staging/v1/dt=2026-10-10/direct-0b9c1f9e-7a3b-4f00-9c1e-2b4d6f8a0c11.ndjson"


def test_whitelist_and_types():
    assert set(clean_record(EVENT)) == set(FIELDS)
    for bad in ({**EVENT, "question": "how many people in Legal"}, {**EVENT, "rows": "five"},
                {**EVENT, "role": "x" * 500}, {"role": "executive"}, [EVENT]):
        with pytest.raises(AuditRecordError):
            clean_record(bad)


def test_fields_equal_the_worker_audit_fields():
    text = (WORKER_DIR / "src" / "audit" / "audit.ts").read_text()
    block = re.search(r"export const AUDIT_FIELDS[^=]*=\s*\[(.*?)\];", text, re.S)
    assert block, "AUDIT_FIELDS not found in worker/src/audit/audit.ts"
    assert re.findall(r'"([a-z_]+)"', block.group(1)) == list(FIELDS)


def test_empty_local_table_tails_nothing(tmp_path):
    assert AuditSink(tmp_path / "ice").tail() == []


def test_sidecar_stage_route(tmp_path, require_warehouse):
    wh = Warehouse(require_warehouse, GENERATED_DIR / "catalog.duckdb.json", "t" * 20, staging_dir=tmp_path / "staging")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(wh))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body, token="t" * 20):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"authorization": f"Bearer {token}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    line = json.dumps(EVENT) + "\n"
    try:
        assert post("/v1/audit/stage", {"key": BATCH_KEY, "body": line}) == (200, {"stored": True})
        assert (tmp_path / "staging" / BATCH_KEY).read_text() == line
        # put-if-absent: content-addressed keys mean a redelivery carries the same bytes
        assert post("/v1/audit/stage", {"key": BATCH_KEY, "body": line}) == (200, {"stored": False})
        assert post("/v1/audit/stage", {"key": DIRECT_KEY, "body": line})[0] == 200
        assert post("/v1/audit/stage", {"key": BATCH_KEY, "body": line}, token="wrong-token-wrong")[0] == 401
        for bad in ("staging/v1/../../x.ndjson", "quarantine/v1/x.ndjson", "staging/v1/dt=2026-10-10/abc.ndjson", BATCH_KEY + ".tmp"):
            status, body = post("/v1/audit/stage", {"key": bad, "body": line})
            assert status == 400 and body["error"]["code"] == "BAD_AUDIT_KEY", bad
        assert post("/v1/audit", {"records": [EVENT]})[0] == 404  # the v1 direct append is gone
        assert sorted(p.name for p in (tmp_path / "staging").rglob("*") if p.is_file()) == sorted(
            [BATCH_KEY.rsplit("/", 1)[1], DIRECT_KEY.rsplit("/", 1)[1]])
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


GET_ACTIVE = ("Getting Basin Catalog status for 'peoplelens-audit'...\n\n"
              "\x1b[37mCatalog URI:\x1b[39m  \x1b[90mhttps://catalog.cloudflarestorage.com/acct123/peoplelens-audit\x1b[39m\n"
              "\x1b[37mWarehouse:\x1b[39m    \x1b[90macct123_peoplelens-audit\x1b[39m\n"
              "\x1b[37mStatus:\x1b[39m       \x1b[90mactive\x1b[39m\n")
GET_DISABLED = "Basin Catalog is not enabled for bucket 'peoplelens-audit'. Please use 'wrangler basin catalog enable peoplelens-audit' first."
ENABLE_OUTPUT = ("Successfully enabled Basin Catalog on bucket 'peoplelens-audit'.\n\n"
                 "Catalog URI: 'https://catalog.cloudflarestorage.com/acct123/peoplelens-audit'\n"
                 "Warehouse: 'acct123_peoplelens-audit'\n\nUse this Catalog URI with Iceberg-compatible query engines.\n")


class CatalogRunner(checks.Runner):
    def __init__(self, enabled: bool):
        super().__init__()
        self.enabled = enabled

    def run(self, cmd, **kw):
        self.log.append(cmd)
        if cmd[2:5] == ["basin", "catalog", "get"]:
            return Result(0, GET_ACTIVE if self.enabled else GET_DISABLED)
        if cmd[2:5] == ["basin", "catalog", "enable"]:
            self.enabled = True
            return Result(0, ENABLE_OUTPUT)
        return Result(0, "")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap_audit, "GENERATED_CONFIG", tmp_path / "wrangler.audit.generated.jsonc")
    monkeypatch.setattr(bootstrap_cloudflare, "DEPLOY_SECRETS", tmp_path / "deploy-secrets.json")
    monkeypatch.setattr(checks, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(bootstrap_audit, "load_state", checks.load_state)
    monkeypatch.setattr(bootstrap_audit, "save_state", checks.save_state)
    return tmp_path


def test_parse_catalog_reads_get_and_enable_output():
    want = {"uri": "https://catalog.cloudflarestorage.com/acct123/peoplelens-audit", "warehouse": "acct123_peoplelens-audit"}
    assert bootstrap_audit.parse_catalog(GET_ACTIVE) == {**want, "status": "active"}
    assert bootstrap_audit.parse_catalog(ENABLE_OUTPUT) == want
    assert bootstrap_audit.parse_catalog(GET_DISABLED) == {}


def test_audit_bootstrap_lists_before_creating_and_never_uses_pipelines(isolated):
    runner = CatalogRunner(enabled=True)
    bootstrap_audit.phase_audit(FakeApi(queues=["peoplelens-audit"], buckets=["peoplelens-audit"]), runner, Plan())
    assert [c for c in runner.log if "create" in c or "enable" in c] == []  # everything existed
    assert not any("pipelines" in c for c in runner.log)
    cfg = json.loads((isolated / "wrangler.audit.generated.jsonc").read_text())
    prod = cfg["env"]["production"]
    assert "pipelines" not in prod and "pipelines" not in cfg
    assert prod["queues"] == {
        "producers": [{"binding": "AUDIT_QUEUE", "queue": "peoplelens-audit"}],
        "consumers": [{"queue": "peoplelens-audit", "max_batch_size": 100, "max_batch_timeout": 30, "max_retries": 10}],
    }
    assert prod["r2_buckets"] == [{"binding": "AUDIT_BUCKET", "bucket_name": "peoplelens-audit"}]
    deploy = runner.log[-1]
    assert deploy[:3] == ["npx", "wrangler", "deploy"] and "--secrets-file" in deploy
    assert deploy[deploy.index("--config") + 1] == "wrangler.audit.generated.jsonc"
    assert ["npm", "run", "build:web"] in runner.log
    assert not (isolated / "deploy-secrets.json").exists()
    state = checks.load_state()
    assert state["audit_enabled"] is True and state["audit_hmac_key_set"] is True
    assert state["audit_catalog_uri"] == "https://catalog.cloudflarestorage.com/acct123/peoplelens-audit"
    assert state["audit_warehouse"] == "acct123_peoplelens-audit"
    # A later plain deploy keeps the audit bindings and does not mint a new HMAC key.
    later = checks.Runner(dry_run=True)
    bootstrap_cloudflare.deploy(later, Plan())
    assert later.log[-1][later.log[-1].index("--config") + 1] == "wrangler.audit.generated.jsonc"
    assert "--secrets-file" not in later.log[-1]


def test_audit_bootstrap_creates_what_is_missing(isolated):
    runner = CatalogRunner(enabled=False)
    bootstrap_audit.phase_audit(FakeApi(), runner, Plan())
    made = [c[:6] for c in runner.log if "create" in c or "enable" in c]
    assert made == [
        ["npx", "wrangler", "queues", "create", "peoplelens-audit"],
        ["npx", "wrangler", "r2", "bucket", "create", "peoplelens-audit"],
        ["npx", "wrangler", "basin", "catalog", "enable", "peoplelens-audit"],
    ]
    assert ["npx", "wrangler", "queues", "create", "peoplelens-audit"] in runner.log  # no retention or delay flags
    assert checks.load_state()["audit_warehouse"] == "acct123_peoplelens-audit"


def test_audit_bootstrap_dry_run_changes_nothing(isolated):
    runner = checks.Runner(dry_run=True)
    runner.run = lambda cmd, **kw: (runner.log.append(cmd), Result(0, GET_DISABLED if cmd[2:5] == ["basin", "catalog", "get"] else ""))[1]
    plan = Plan()
    bootstrap_audit.phase_audit(FakeApi(), runner, plan)
    assert not (isolated / "wrangler.audit.generated.jsonc").exists()
    assert checks.load_state() == {}
    assert any("deploy with AUDIT_HMAC_KEY" in s for s in plan.steps)
