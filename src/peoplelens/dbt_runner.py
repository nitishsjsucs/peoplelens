"""`pl dbt` and `pl mf validate`: the only supported way to run dbt here.

Each dialect has its own committed profiles directory with its own default
target, because MetricFlow's CLIConfiguration always uses the profile's
default target and reads <project>/target/semantic_manifest.json.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from peoplelens.paths import DBT_PROFILES_ROOT, DBT_PROJECT_DIR, DUCKDB_PATH, WAREHOUSE_DIR

DIALECTS = ("duckdb", "bigquery_compile", "bigquery")
GCP_PROJECT_RE = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"


class DbtRunnerError(RuntimeError):
    pass


def venv_bin(name: str) -> str:
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    return name


def profiles_dir(dialect: str) -> Path:
    if dialect not in DIALECTS:
        raise DbtRunnerError(f"unknown dialect {dialect!r}; expected one of {', '.join(DIALECTS)}")
    return DBT_PROFILES_ROOT / dialect


def dbt_env(dialect: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a dbt child process of this dialect."""
    import re

    env = dict(os.environ if base is None else base)
    env["PL_DUCKDB_PATH"] = str(DUCKDB_PATH.resolve())
    env.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    env.pop("DBT_TARGET", None)
    env.pop("DBT_PROFILES_DIR", None)
    if dialect == "bigquery":
        project = env.get("GCP_PROJECT_ID", "")
        if not project:
            raise DbtRunnerError(
                "GCP_PROJECT_ID is not set. Export it (for example `export GCP_PROJECT_ID=peoplelens-<suffix>`) "
                "or run through `uv run pl-ops bootstrap`, which sets it from .secrets/bootstrap-state.json."
            )
        if not re.match(GCP_PROJECT_RE, project):
            raise DbtRunnerError(f"GCP_PROJECT_ID {project!r} is not a valid Google Cloud project id")
    return env


def dbt_command(dialect: str, args: list[str]) -> list[str]:
    if not args:
        raise DbtRunnerError("pass a dbt command, for example `pl dbt --dialect duckdb build`")
    return [
        venv_bin("dbt"),
        *args,
        "--project-dir",
        str(DBT_PROJECT_DIR),
        "--profiles-dir",
        str(profiles_dir(dialect)),
    ]


def run_dbt(dialect: str, args: list[str], *, capture: bool = False, base_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run dbt for a dialect. Creates .dev/warehouse/ first (dbt-duckdb does not)."""
    WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = dbt_command(dialect, args)
    env = dbt_env(dialect, base_env)
    return subprocess.run(
        cmd,
        cwd=str(DBT_PROJECT_DIR),
        env=env,
        check=False,
        text=True,
        capture_output=capture,
    )


def run_mf_validate() -> int:
    """`mf validate-configs --skip-dw` after a DuckDB parse, with cwd warehouse/dbt."""
    parsed = run_dbt("duckdb", ["parse", "--no-partial-parse"])
    if parsed.returncode != 0:
        return parsed.returncode
    env = dbt_env("duckdb")
    env["DBT_PROFILES_DIR"] = str(profiles_dir("duckdb"))
    proc = subprocess.run(
        [venv_bin("mf"), "validate-configs", "--skip-dw"],
        cwd=str(DBT_PROJECT_DIR),
        env=env,
        check=False,
    )
    return proc.returncode
