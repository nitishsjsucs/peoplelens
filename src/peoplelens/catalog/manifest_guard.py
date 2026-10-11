"""Guard against rendering SQL from the wrong dialect's manifest (SPEC.md section 8).

MetricFlow's CLIConfiguration always reads <project>/target/semantic_manifest.json.
A BigQuery-parsed manifest combined with the DuckDB profile renders DuckDB SQL
against BigQuery relations without any error, so before rendering we assert the
adapter type and every semantic model's relation database.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

EXPECTED = {
    "duckdb": {"adapter": "duckdb", "database": "peoplelens"},
    "bigquery_compile": {"adapter": "bigquery", "database": "peoplelens-project-id"},
}


class ManifestGuardError(RuntimeError):
    pass


def rerun_hint(dialect: str) -> str:
    return f"uv run pl dbt --dialect {dialect} parse --no-partial-parse"


def check(dialect: str, adapter_type: str, relation_databases: list[tuple[str, str]]) -> None:
    """Raise ManifestGuardError unless the adapter and every relation database match the dialect."""
    exp = EXPECTED[dialect]
    problems = []
    if adapter_type != exp["adapter"]:
        problems.append(f"adapter is {adapter_type!r}, expected {exp['adapter']!r}")
    for name, database in relation_databases:
        if database != exp["database"]:
            problems.append(f"semantic model {name!r} points at database {database!r}, expected {exp['database']!r}")
    if not relation_databases:
        problems.append("the semantic manifest has no semantic models")
    if problems:
        raise ManifestGuardError(
            "manifest guard failed for dialect "
            + dialect
            + ": "
            + "; ".join(problems)
            + f". Rerun `{rerun_hint(dialect)}` and compile again."
        )


def manifest_sha256(project_dir: Path) -> str:
    return hashlib.sha256((project_dir / "target" / "semantic_manifest.json").read_bytes()).hexdigest()
