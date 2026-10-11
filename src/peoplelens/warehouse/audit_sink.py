"""Audit table schema, whitelist and catalogs for the metadata-only audit trail.

The Iceberg table is peoplelens.audit_events: a pyiceberg SqlCatalog (sqlite,
.dev/iceberg) locally, Basin Catalog (Iceberg REST) in production. Rows reach it
only through the committer (warehouse/audit_commit.py), which validates every
staged line with clean_record below.

Only the whitelisted fields below are accepted, with their types checked, so a
question text, an email or a result value can never be written. A test keeps
FIELDS equal to the Worker's AUDIT_FIELDS (worker/src/audit/audit.ts).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pyarrow as pa

from peoplelens.paths import ICEBERG_DIR
NAMESPACE = "peoplelens"
TABLE = "audit_events"

STRING = "string"
LONG = "long"
DOUBLE = "double"
FIELDS: dict[str, str] = {
    "ts": STRING,
    "request_id": STRING,
    "subject_hmac": STRING,
    "role": STRING,
    "action": STRING,
    "metric": STRING,
    "group_by": STRING,
    "grain": STRING,
    "filter_dimensions": STRING,
    "filter_values": STRING,
    "catalog_key": STRING,
    "decision": STRING,
    "rows": LONG,
    "suppressed_primary": LONG,
    "suppressed_complementary": LONG,
    "suppressed_hidden_sum": LONG,
    "suppressed_complement": LONG,
    "estimated_bytes": LONG,
    "billed_bytes": LONG,
    "latency_ms": DOUBLE,
    "llm_ms": DOUBLE,
    "warehouse_ms": DOUBLE,
    "provider": STRING,
    "model": STRING,
    "gateway_log_id": STRING,
}
MAX_STRING = 200


class AuditRecordError(ValueError):
    pass


def arrow_schema() -> pa.Schema:
    types = {STRING: pa.string(), LONG: pa.int64(), DOUBLE: pa.float64()}
    return pa.schema([pa.field(name, types[kind], nullable=True) for name, kind in FIELDS.items()])


def clean_record(raw: object) -> dict:
    """Validate one event: only known keys, scalar values of the declared type."""
    if not isinstance(raw, dict):
        raise AuditRecordError("record must be an object")
    unknown = set(raw) - set(FIELDS)
    if unknown:
        raise AuditRecordError(f"unknown fields: {sorted(unknown)}")
    out: dict = {}
    for name, kind in FIELDS.items():
        v = raw.get(name)
        if v is None:
            out[name] = None
        elif kind == STRING:
            if not isinstance(v, str) or len(v) > MAX_STRING:
                raise AuditRecordError(f"{name} must be a string of at most {MAX_STRING} characters")
            out[name] = v
        elif kind == LONG:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise AuditRecordError(f"{name} must be a number")
            out[name] = int(v)
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise AuditRecordError(f"{name} must be a number")
            out[name] = float(v)
    if not out["ts"] or not out["request_id"]:
        raise AuditRecordError("ts and request_id are required")
    return out


def local_catalog(root: Path = ICEBERG_DIR):
    """The local stand-in for Basin Catalog: pyiceberg SqlCatalog on sqlite."""
    from pyiceberg.catalog.sql import SqlCatalog

    root.mkdir(parents=True, exist_ok=True)
    (root / "warehouse").mkdir(exist_ok=True)
    return SqlCatalog(
        "peoplelens_local",
        uri=f"sqlite:///{root / 'catalog.db'}",
        warehouse=(root / "warehouse").resolve().as_uri(),
    )


def load_or_create_table(catalog):
    """peoplelens.audit_events (unpartitioned; schema from FIELDS), created on first use."""
    from pyiceberg.exceptions import NamespaceAlreadyExistsError, TableAlreadyExistsError

    try:
        catalog.create_namespace(NAMESPACE)
    except NamespaceAlreadyExistsError:
        pass
    try:
        return catalog.create_table(f"{NAMESPACE}.{TABLE}", schema=arrow_schema())
    except TableAlreadyExistsError:
        return catalog.load_table(f"{NAMESPACE}.{TABLE}")


class AuditSink:
    """Read access to the local audit table (`pl audit tail`)."""

    def __init__(self, root: Path = ICEBERG_DIR):
        self.catalog = local_catalog(root)
        self._lock = threading.Lock()

    def table(self):
        return load_or_create_table(self.catalog)

    def tail(self, limit: int = 20) -> list[dict]:
        with self._lock:
            table = self.table().scan().to_arrow()
        rows = table.to_pylist()
        rows.sort(key=lambda r: r["ts"] or "")
        return rows[-limit:]
