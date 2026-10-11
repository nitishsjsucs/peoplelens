"""Local model of BigQuery on-demand billing (an estimate, labeled as such).

For each base table scanned in DuckDB's `EXPLAIN (FORMAT json)` plan, the
estimate is table rows x the sum of BigQuery logical sizes of the columns the
query reads (projected or filtered), then max(10 MB, bytes) per referenced
table, which is BigQuery's documented minimum. Logical sizes: INT64, FLOAT64,
DATE, TIMESTAMP 8 bytes; BOOL 1; NUMERIC 16; STRING 2 + average UTF-8 length.

Unpartitioned BigQuery tables bill every row of every column read whatever the
filters, so the estimate does not depend on parameter values.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field

BQ_MIN_BYTES_PER_TABLE = 10 * 1024 * 1024

FIXED_SIZES = {
    "BIGINT": 8, "INTEGER": 8, "SMALLINT": 8, "TINYINT": 8, "HUGEINT": 8, "UBIGINT": 8, "UINTEGER": 8,
    "DOUBLE": 8, "FLOAT": 8, "REAL": 8,
    "DATE": 8, "TIMESTAMP": 8, "TIMESTAMP WITH TIME ZONE": 8, "TIME": 8,
    "BOOLEAN": 1,
}


def logical_size(duckdb_type: str, avg_utf8_len: float = 0.0) -> float:
    t = duckdb_type.upper()
    if t in ("VARCHAR", "TEXT", "STRING") or t.startswith("VARCHAR"):
        return 2.0 + avg_utf8_len
    if t.startswith("DECIMAL"):
        return 16.0
    if t in FIXED_SIZES:
        return float(FIXED_SIZES[t])
    raise ValueError(f"no BigQuery logical size for DuckDB type {duckdb_type!r}")


@dataclass
class TableStats:
    rows: int
    column_sizes: dict[str, float]


@dataclass
class Estimate:
    total_bytes: int
    tables: dict[str, dict] = field(default_factory=dict)
    note: str = "estimate: local model of BigQuery on-demand billing, not a BigQuery dry run"


def _scans(node: dict) -> list[dict]:
    out = []
    name = node.get("name") or node.get("operator_name") or ""
    if "SCAN" in str(name).upper():
        extra = node.get("extra_info") or {}
        if isinstance(extra, dict) and extra.get("Table"):
            out.append(extra)
    for child in node.get("children", []) or []:
        out.extend(_scans(child))
    return out


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


class ByteEstimator:
    """Thread-safe estimator over one DuckDB database. Pass a cursor factory."""

    def __init__(self, cursor_factory):
        self._cursor = cursor_factory
        self._lock = threading.Lock()
        self._table_cache: dict[str, TableStats] = {}
        self._sql_cache: dict[str, Estimate] = {}

    def table_stats(self, qualified: str) -> TableStats:
        with self._lock:
            if qualified in self._table_cache:
                return self._table_cache[qualified]
        parts = qualified.split(".")
        catalog, schema, table = (parts + ["", "", ""])[:3] if len(parts) == 3 else ("", parts[0], parts[1])
        cur = self._cursor()
        cols = cur.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_catalog = ? and table_schema = ? and table_name = ? order by ordinal_position",
            [catalog, schema, table],
        ).fetchall()
        if not cols:
            raise ValueError(f"unknown table {qualified!r}")
        ident = ".".join(f'"{p}"' for p in parts)
        rows = cur.execute(f"select count(*) from {ident}").fetchone()[0]
        sizes: dict[str, float] = {}
        for name, dtype in cols:
            avg = 0.0
            if dtype.upper().startswith("VARCHAR"):
                avg = cur.execute(f'select coalesce(avg(strlen("{name}")), 0) from {ident}').fetchone()[0]
            sizes[name] = logical_size(dtype, float(avg))
        stats = TableStats(rows=int(rows), column_sizes=sizes)
        with self._lock:
            self._table_cache[qualified] = stats
        return stats

    def estimate(self, sql: str, params: dict | None = None, cache_key: str | None = None) -> Estimate:
        if cache_key is not None:
            with self._lock:
                if cache_key in self._sql_cache:
                    return self._sql_cache[cache_key]
        cur = self._cursor()
        rows = cur.execute("EXPLAIN (FORMAT json) " + sql, params or {}).fetchall()
        plan = json.loads(rows[0][1])
        scans: list[dict] = []
        for root in plan if isinstance(plan, list) else [plan]:
            scans.extend(_scans(root))
        read: dict[str, set[str]] = {}
        for scan in scans:
            table = scan["Table"]
            stats = self.table_stats(table)
            cols = read.setdefault(table, set())
            for proj in _as_list(scan.get("Projections")):
                if proj in stats.column_sizes:
                    cols.add(proj)
            filters = " ".join(_as_list(scan.get("Filters")))
            for col in stats.column_sizes:
                if re.search(rf"\b{re.escape(col)}\b", filters):
                    cols.add(col)
        total = 0
        detail: dict[str, dict] = {}
        for table in sorted(read):
            stats = self.table_stats(table)
            raw = int(round(stats.rows * sum(stats.column_sizes[c] for c in read[table])))
            billed = max(BQ_MIN_BYTES_PER_TABLE, raw)
            detail[table] = {"rows": stats.rows, "columns": sorted(read[table]), "raw_bytes": raw, "billed_bytes": billed}
            total += billed
        est = Estimate(total_bytes=total, tables=detail)
        if cache_key is not None:
            with self._lock:
                self._sql_cache[cache_key] = est
        return est
