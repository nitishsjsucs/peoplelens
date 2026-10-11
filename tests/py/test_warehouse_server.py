"""Local warehouse sidecar: token, catalog allowlist, parameter contract, read-only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import date

import duckdb
import pytest

from peoplelens.catalog.params import bind_values
from peoplelens.paths import GENERATED_DIR
from peoplelens.policy import load
from peoplelens.warehouse.cost import BQ_MIN_BYTES_PER_TABLE
from peoplelens.warehouse.server import ThreadingHTTPServer, Warehouse, make_handler

TOKEN = "test-token-0123456789abcdef"
POLICY = load()
CATALOG = json.loads((GENERATED_DIR / "catalog.duckdb.json").read_text())
ENTRY = CATALOG["entries"]["headcount|department|none"]


@pytest.fixture(scope="module")
def server(require_warehouse):
    wh = Warehouse(require_warehouse, GENERATED_DIR / "catalog.duckdb.json", TOKEN)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(wh))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield wh, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    wh.close()


def _params(entry=ENTRY, **kw):
    values = bind_values(POLICY, entry["metric"], date(2025, 12, 1), date(2025, 12, 31), **kw)
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in values.items()}


def post(base, body, token=TOKEN, path="/v1/query"):
    data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST", headers={"content-type": "application/json"})
    if token is not None:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_has_no_data(server):
    _, base = server
    with urllib.request.urlopen(base + "/v1/health", timeout=5) as resp:
        body = json.loads(resp.read())
    assert body == {"ok": True, "catalog_entries": 312, "read_only": True}


def test_requires_bearer_token(server):
    _, base = server
    assert post(base, {"sql": ENTRY["sql"], "params": _params()}, token=None)[0] == 401
    assert post(base, {"sql": ENTRY["sql"], "params": _params()}, token="wrong-token-xxxxxxxx")[0] == 401


def test_refuses_sql_not_in_catalog(server):
    _, base = server
    for sql in ("select 1", "CREATE TABLE core.x (a int)", ENTRY["sql"] + " "):
        status, body = post(base, {"sql": sql, "params": {}})
        assert status == 403 and body["error"]["code"] == "SQL_NOT_IN_CATALOG"


def test_parameter_contract_is_exact(server):
    _, base = server
    good = _params()
    missing = dict(good)
    missing.pop("f_location")
    extra = dict(good, f_gender=["F"])
    null_value = dict(good, f_location=None)
    empty = dict(good, f_location=[])
    bad_date = dict(good, start_ts="2025-13-01")
    for params in (missing, extra, null_value, empty, bad_date):
        status, body = post(base, {"sql": ENTRY["sql"], "params": params})
        assert status == 400 and body["error"]["code"] == "BAD_PARAMS", params


def test_executes_catalog_sql_and_matches_direct_execution(server, require_warehouse):
    _, base = server
    status, body = post(base, {"sql": ENTRY["sql"], "params": _params()})
    assert status == 200
    assert body["catalog_key"] == "headcount|department|none"
    got = {row[body["columns"].index("workforce__department")]: row[body["columns"].index("headcount")] for row in body["rows"]}
    assert got["Legal"] == 8 and got["Engineering"] == 620 and sum(got.values()) == 1780
    assert body["estimated_bytes"] >= BQ_MIN_BYTES_PER_TABLE
    assert "estimate" in body["estimate_note"]


def test_scope_is_a_bound_parameter(server):
    _, base = server
    status, body = post(base, {"sql": ENTRY["sql"], "params": _params(scope_departments=["Engineering", "Sales"])})
    assert status == 200
    depts = {row[body["columns"].index("workforce__department")] for row in body["rows"]}
    assert depts == {"Engineering", "Sales"}


def test_dry_run_returns_estimate_only(server):
    _, base = server
    status, body = post(base, {"sql": ENTRY["sql"], "params": _params(), "dry_run": True})
    assert status == 200 and body["rows"] == [] and body["estimated_bytes"] >= BQ_MIN_BYTES_PER_TABLE


def test_connection_is_read_only(server):
    wh, _ = server
    with pytest.raises(duckdb.Error, match="read-only|Cannot execute statement of type CREATE"):
        wh.cursor().execute("CREATE TABLE core.should_fail (a INTEGER)")


def test_short_token_is_rejected(require_warehouse):
    with pytest.raises(ValueError):
        Warehouse(require_warehouse, GENERATED_DIR / "catalog.duckdb.json", "short")


def test_cli_prints_ephemeral_port_and_stops_on_sigterm(require_warehouse):
    env = dict(os.environ, PL_WAREHOUSE_TOKEN=TOKEN)
    proc = subprocess.Popen(
        [sys.executable, "-m", "peoplelens.cli", "warehouse", "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        line = proc.stdout.readline()
        port = json.loads(line)["listening"]
        assert port > 0
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=5) as resp:
            assert json.loads(resp.read())["ok"] is True
    finally:
        proc.terminate()
        assert proc.wait(timeout=10) == 0
