"""Audit committer: staged NDJSON objects into the Iceberg table peoplelens.audit_events.

Workers Free plan design (SPEC.md 25.4 A; Basin Pipelines is Workers Paid only). The
Worker stages metadata-only audit events as immutable NDJSON objects:

    staging/v1/dt=<day>/<32 hex>.ndjson         one queue-consumer batch (content-addressed)
    staging/v1/dt=<day>/direct-<request_id>.ndjson   one event put without the queue

This module commits them, the same code locally and in production:

- staging store: a local directory (.dev/audit-staging) or the R2 bucket
  peoplelens-audit through pyarrow's S3 filesystem; both are a pyarrow FileSystem;
- catalog: pyiceberg SqlCatalog (sqlite, .dev/iceberg) locally, RestCatalog
  against Basin Catalog in production.

One run: list the staged objects; validate every line with the audit whitelist
(an object with any invalid line is copied to quarantine/ and removed from staging,
never partly committed); deduplicate on request_id within the run; anti-join against
table rows whose ts falls in [earliest staged day - 2 days, latest day + 1 day];
append the new rows in ONE snapshot (snapshot properties record how many staged
objects and a hash of their keys); only then delete the committed staging objects.
On an Iceberg commit conflict it reloads the table and repeats the dedupe, up to 3
times. Re-running over the same objects (for example after a crash between the append
and the deletes) appends nothing: delivery is at least once, the table effect exactly once.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.fs as pafs

from peoplelens.warehouse.audit_sink import NAMESPACE, TABLE, AuditRecordError, arrow_schema, clean_record, load_or_create_table

STAGING_PREFIX = "staging/v1/"
QUARANTINE_PREFIX = "quarantine/v1/"
BATCH_KEY = re.compile(r"^staging/v1/dt=\d{4}-\d{2}-\d{2}/[0-9a-f]{32}\.ndjson$")
DIRECT_KEY = re.compile(r"^staging/v1/dt=\d{4}-\d{2}-\d{2}/direct-[A-Za-z0-9-]{1,64}\.ndjson$")
MAX_COMMIT_ATTEMPTS = 3
WINDOW_BEFORE_DAYS = 2
WINDOW_AFTER_DAYS = 1
SNAPSHOT_OBJECTS = "peoplelens.staged-objects"
SNAPSHOT_SHA256 = "peoplelens.staged-sha256"


def valid_key(key: str) -> bool:
    return bool(BATCH_KEY.match(key) or DIRECT_KEY.match(key))


class FsStagingStore:
    """Staging objects on a pyarrow FileSystem under `base` (a directory, or a bucket name on S3/R2)."""

    def __init__(self, fs: pafs.FileSystem, base: str, kind: str):
        self.fs = fs
        self.base = base.rstrip("/")
        self.kind = kind

    def _path(self, key: str) -> str:
        return f"{self.base}/{key}"

    def list(self, prefix: str = STAGING_PREFIX) -> list[str]:
        selector = pafs.FileSelector(self._path(prefix), recursive=True, allow_not_found=True)
        keys = []
        for info in self.fs.get_file_info(selector):
            if info.type == pafs.FileType.File:
                keys.append(info.path[len(self.base) + 1:])
        return sorted(keys)

    def read(self, key: str) -> bytes:
        with self.fs.open_input_stream(self._path(key)) as f:
            return f.read()

    def put(self, key: str, data: bytes) -> None:
        if self.kind == "local":
            self.fs.create_dir(self._path(key).rsplit("/", 1)[0], recursive=True)
        with self.fs.open_output_stream(self._path(key)) as f:
            f.write(data)

    def delete(self, key: str) -> None:
        self.fs.delete_file(self._path(key))


def local_store(root: Path) -> FsStagingStore:
    root.mkdir(parents=True, exist_ok=True)
    return FsStagingStore(pafs.LocalFileSystem(), str(root.resolve()), "local")


def r2_store(account_id: str, access_key_id: str, secret_access_key: str, bucket: str) -> FsStagingStore:
    """R2 through its S3-compatible API (https://<account>.r2.cloudflarestorage.com, region auto)."""
    fs = pafs.S3FileSystem(
        access_key=access_key_id,
        secret_key=secret_access_key,
        endpoint_override=f"{account_id}.r2.cloudflarestorage.com",
        scheme="https",
        region="auto",
    )
    return FsStagingStore(fs, bucket, "r2")


@dataclass
class CommitResult:
    objects: int = 0
    events: int = 0
    duplicates_in_run: int = 0
    already_in_table: int = 0
    appended: int = 0
    quarantined: list[str] = field(default_factory=list)
    deleted: int = 0
    attempts: int = 0
    snapshot_id: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def parse_object(data: bytes) -> list[dict]:
    """Every line must pass the whitelist; raises AuditRecordError otherwise."""
    rows = []
    for n, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditRecordError(f"line {n} is not JSON") from exc
        rows.append(clean_record(raw))
    if not rows:
        raise AuditRecordError("empty object")
    return rows


def _day(ts: str) -> date | None:
    try:
        return date.fromisoformat(ts[:10])
    except ValueError:
        return None


def existing_ids(table, rows: list[dict]) -> set[str]:
    """request_ids already in the table among rows whose ts lies in the staged days' window."""
    from pyiceberg.expressions import And, GreaterThanOrEqual, LessThan

    days = [d for d in (_day(r["ts"]) for r in rows) if d is not None]
    if not days:
        return set()
    lo = (min(days) - timedelta(days=WINDOW_BEFORE_DAYS)).isoformat()
    hi = (max(days) + timedelta(days=WINDOW_AFTER_DAYS + 1)).isoformat()
    scan = table.scan(row_filter=And(GreaterThanOrEqual("ts", lo), LessThan("ts", hi)), selected_fields=("request_id",))
    return {r for r in scan.to_arrow().column("request_id").to_pylist() if r is not None}


def commit(store: FsStagingStore, catalog, *, sleep=time.sleep) -> CommitResult:
    from pyiceberg.exceptions import CommitFailedException

    result = CommitResult()
    keys = [k for k in store.list(STAGING_PREFIX) if valid_key(k)]
    committed_keys: list[str] = []
    by_id: dict[str, dict] = {}
    for key in keys:
        data = store.read(key)
        try:
            rows = parse_object(data)
        except AuditRecordError:
            store.put(QUARANTINE_PREFIX + key[len(STAGING_PREFIX):], data)
            store.delete(key)
            result.quarantined.append(key)
            continue
        committed_keys.append(key)
        for row in rows:
            result.events += 1
            if row["request_id"] in by_id:
                result.duplicates_in_run += 1
            else:
                by_id[row["request_id"]] = row
    result.objects = len(committed_keys)
    candidates = list(by_id.values())
    table = load_or_create_table(catalog)
    props = {
        SNAPSHOT_OBJECTS: str(len(committed_keys)),
        SNAPSHOT_SHA256: hashlib.sha256("\n".join(sorted(committed_keys)).encode()).hexdigest(),
    }
    for attempt in range(1, MAX_COMMIT_ATTEMPTS + 1):
        result.attempts = attempt
        seen = existing_ids(table, candidates) if candidates else set()
        new_rows = sorted((r for r in candidates if r["request_id"] not in seen), key=lambda r: (r["ts"], r["request_id"]))
        result.already_in_table = len(candidates) - len(new_rows)
        if not new_rows:
            result.appended = 0
            break
        try:
            table.append(pa.Table.from_pylist(new_rows, schema=arrow_schema()), snapshot_properties=props)
            result.appended = len(new_rows)
            snap = table.current_snapshot()
            result.snapshot_id = snap.snapshot_id if snap else None
            break
        except CommitFailedException:
            if attempt == MAX_COMMIT_ATTEMPTS:
                raise
            sleep(0.5 * attempt)
            table = catalog.load_table(f"{NAMESPACE}.{TABLE}")
    for key in committed_keys:
        store.delete(key)
        result.deleted += 1
    return result


def staged_ids(store: FsStagingStore) -> set[str]:
    """Unique request_ids in the valid staged objects (used to wait for a queue to drain)."""
    ids: set[str] = set()
    for key in store.list(STAGING_PREFIX):
        if not valid_key(key):
            continue
        try:
            ids.update(r["request_id"] for r in parse_object(store.read(key)))
        except (AuditRecordError, OSError):
            continue
    return ids
