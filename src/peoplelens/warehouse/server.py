"""`pl warehouse serve`: the local warehouse sidecar (a stand-in for BigQuery, local only).

DuckDB cannot run inside workerd, so `wrangler dev` reaches the local DuckDB
file through this small HTTP server:

- binds 127.0.0.1 on an ephemeral port (port 0) and prints {"listening": <port>}
- requires `Authorization: Bearer <PL_WAREHOUSE_TOKEN>` on every data route
- opens DuckDB with read_only=True; every request uses its own cursor
- executes only SQL whose sha256 is in the compiled DuckDB catalog, with exactly
  the parameters that entry declares

POST /v1/query  {sql, params, dry_run} -> {columns, rows, estimated_bytes, elapsed_ms, estimate_note}
POST /v1/audit  {records: [...]} -> {appended}   (metadata-only events into a local Iceberg table)
GET  /v1/health -> {ok, catalog_entries}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb

from peoplelens.paths import DUCKDB_PATH, GENERATED_DIR
from peoplelens.warehouse.cost import ByteEstimator

MAX_BODY_BYTES = 256 * 1024
MAX_RESULT_ROWS = 5000
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TOKEN_ENV = "PL_WAREHOUSE_TOKEN"


class RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def load_allowlist(catalog_path: Path) -> dict[str, dict]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog["header"]["dialect"] != "duckdb":
        raise ValueError(f"{catalog_path} is not a DuckDB catalog")
    return {e["sha256"]: e for e in catalog["entries"].values()}


def coerce_params(entry: dict, raw: object) -> dict[str, object]:
    """Exactly the declared parameters; time params are ISO dates, arrays are non-empty string lists."""
    if not isinstance(raw, dict):
        raise RequestError(400, "BAD_PARAMS", "params must be an object")
    declared = {p["name"]: p["type"] for p in entry["params"]}
    if set(raw) != set(declared):
        missing = sorted(set(declared) - set(raw))
        extra = sorted(set(raw) - set(declared))
        raise RequestError(400, "BAD_PARAMS", f"parameter mismatch; missing {missing}, unexpected {extra}")
    out: dict[str, object] = {}
    for name, ptype in declared.items():
        value = raw[name]
        if ptype == "DATE":
            if not isinstance(value, str) or not ISO_DATE.match(value):
                raise RequestError(400, "BAD_PARAMS", f"{name} must be an ISO date (YYYY-MM-DD)")
            try:
                out[name] = date.fromisoformat(value)
            except ValueError as exc:
                raise RequestError(400, "BAD_PARAMS", f"{name}: {exc}") from exc
        else:
            if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
                raise RequestError(400, "BAD_PARAMS", f"{name} must be a non-empty array of strings")
            out[name] = list(value)
    return out


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


class Warehouse:
    """Shared state: one read-only DuckDB connection, the allowlist and the estimator."""

    def __init__(self, db_path: Path, catalog_path: Path, token: str):
        if not token or len(token) < 16:
            raise ValueError(f"{TOKEN_ENV} must be set to a random token of at least 16 characters")
        self.token = token
        self.allowlist = load_allowlist(catalog_path)
        self.con = duckdb.connect(str(db_path), read_only=True)
        self._con_lock = threading.Lock()
        self.estimator = ByteEstimator(self.cursor)
        self._audit = None

    def cursor(self) -> duckdb.DuckDBPyConnection:
        # A DuckDB connection is not safe to share across threads; cursors are.
        with self._con_lock:
            return self.con.cursor()

    def authorized(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):].encode(), self.token.encode())

    def query(self, body: dict) -> dict:
        sql = body.get("sql")
        if not isinstance(sql, str) or not sql:
            raise RequestError(400, "BAD_REQUEST", "sql is required")
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        entry = self.allowlist.get(digest)
        if entry is None:
            raise RequestError(403, "SQL_NOT_IN_CATALOG", "only compiled catalog SQL is executed")
        params = coerce_params(entry, body.get("params"))
        estimate = self.estimator.estimate(sql, params, cache_key=digest)
        result: dict[str, object] = {
            "catalog_key": entry["key"],
            "estimated_bytes": estimate.total_bytes,
            "estimate_note": estimate.note,
        }
        if body.get("dry_run"):
            result.update({"columns": [], "rows": [], "elapsed_ms": 0})
            return result
        started = time.perf_counter()
        cur = self.cursor()
        try:
            cur.execute(sql, params)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(MAX_RESULT_ROWS + 1)
        except duckdb.Error as exc:
            raise RequestError(500, "WAREHOUSE_ERROR", type(exc).__name__) from exc
        finally:
            cur.close()
        if len(rows) > MAX_RESULT_ROWS:
            raise RequestError(422, "RESULT_TOO_LARGE", f"more than {MAX_RESULT_ROWS} rows")
        result.update(
            {
                "columns": columns,
                "rows": [[_jsonable(v) for v in row] for row in rows],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        return result

    def audit(self, body: dict) -> dict:
        """Append metadata-only audit events to the local Iceberg table (pyiceberg SqlCatalog)."""
        from peoplelens.warehouse.audit_sink import AuditRecordError, AuditSink

        records = body.get("records")
        if not isinstance(records, list) or len(records) > 500:
            raise RequestError(400, "BAD_REQUEST", "records must be a list of at most 500 events")
        with self._con_lock:
            if self._audit is None:
                self._audit = AuditSink()
        try:
            return {"appended": self._audit.append(records)}
        except AuditRecordError as exc:
            raise RequestError(400, "BAD_AUDIT_RECORD", str(exc)) from exc

    def close(self) -> None:
        self.con.close()


def make_handler(warehouse: Warehouse):
    class Handler(BaseHTTPRequestHandler):
        server_version = "peoplelens-warehouse/1"
        sys_version = ""
        # HTTP/1.1 keep-alive: workerd pools connections, and an HTTP/1.0 server
        # closing after every response races with that reuse ("Network connection lost").
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:  # no request logging (no SQL, no params)
            return

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/health":
                self._send(200, {"ok": True, "catalog_entries": len(warehouse.allowlist), "read_only": True})
            else:
                self._send(404, {"error": {"code": "NOT_FOUND"}})

        def do_POST(self) -> None:  # noqa: N802
            try:
                if not warehouse.authorized(self.headers.get("authorization")):
                    raise RequestError(401, "UNAUTHENTICATED", "missing or wrong bearer token")
                length = int(self.headers.get("content-length") or 0)
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise RequestError(413 if length > MAX_BODY_BYTES else 400, "BAD_REQUEST", "bad body length")
                try:
                    body = json.loads(self.rfile.read(length))
                except json.JSONDecodeError as exc:
                    raise RequestError(400, "BAD_REQUEST", "body is not JSON") from exc
                if not isinstance(body, dict):
                    raise RequestError(400, "BAD_REQUEST", "body must be an object")
                if self.path == "/v1/query":
                    self._send(200, warehouse.query(body))
                elif self.path == "/v1/audit":
                    self._send(200, warehouse.audit(body))
                else:
                    raise RequestError(404, "NOT_FOUND", "unknown route")
            except RequestError as exc:
                self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})

    return Handler


def serve(port: int = 0, db_path: Path = DUCKDB_PATH, catalog_path: Path | None = None,
          token: str | None = None, ready_stream=None) -> None:
    token = token if token is not None else os.environ.get(TOKEN_ENV, "")
    catalog_path = catalog_path or (GENERATED_DIR / "catalog.duckdb.json")
    warehouse = Warehouse(db_path, catalog_path, token)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(warehouse))
    httpd.daemon_threads = True
    stream = ready_stream or sys.stdout
    stream.write(json.dumps({"listening": httpd.server_address[1]}) + "\n")
    stream.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        warehouse.close()
