"""Turn warehouse rows for a catalog entry into comparable cells."""

from __future__ import annotations

from datetime import date, datetime


def iso_bucket(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return text[:10]


def rows_to_cells(entry: dict, columns: list[str], rows: list[tuple]) -> list[dict]:
    """Map raw result rows to {dims, period, values, cohort}; drops empty-aggregate rows (cohort NULL)."""
    idx = {c: i for i, c in enumerate(columns)}
    colmap = entry["columns"]
    out = []
    for row in rows:
        cohort = row[idx[colmap["cohort"]]]
        if cohort is None:
            continue
        out.append(
            {
                "dims": {d: row[idx[c]] for d, c in colmap["dimensions"].items()},
                "period": iso_bucket(row[idx[colmap["time"]]]) if colmap["time"] else None,
                "values": {m: (None if row[idx[m]] is None else float(row[idx[m]])) for m in colmap["values"]},
                "cohort": float(cohort),
            }
        )
    return out
