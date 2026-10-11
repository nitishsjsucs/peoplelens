"""The manifest guard rejects SQL rendering from the wrong dialect's manifest."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from peoplelens.catalog import manifest_guard
from peoplelens.dbt_runner import dbt_env, venv_bin
from peoplelens.paths import DBT_PROJECT_DIR


def test_matching_manifest_passes():
    manifest_guard.check("duckdb", "duckdb", [("workforce", "peoplelens"), ("offers", "peoplelens")])
    manifest_guard.check(
        "bigquery_compile", "bigquery", [("workforce", "peoplelens-project-id")]
    )


def test_wrong_adapter_or_database_fails_with_rerun_command():
    with pytest.raises(manifest_guard.ManifestGuardError) as exc:
        manifest_guard.check("duckdb", "duckdb", [("workforce", "peoplelens-project-id")])
    assert "uv run pl dbt --dialect duckdb parse --no-partial-parse" in str(exc.value)
    with pytest.raises(manifest_guard.ManifestGuardError):
        manifest_guard.check("bigquery_compile", "duckdb", [("workforce", "peoplelens-project-id")])
    with pytest.raises(manifest_guard.ManifestGuardError):
        manifest_guard.check("duckdb", "duckdb", [])


def test_bigquery_manifest_with_duckdb_profile_is_rejected(tmp_path):
    """Reproduces the silent-mismatch hazard end to end on a copy of the dbt project:
    parse with the BigQuery compile profile, then set MetricFlow up with the DuckDB
    profile. MetricFlow itself raises nothing; the guard must."""
    project = tmp_path / "dbt"
    shutil.copytree(DBT_PROJECT_DIR, project, ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"))
    env = dbt_env("bigquery_compile")
    env["PL_DUCKDB_PATH"] = str(tmp_path / "unused.duckdb")
    parse = subprocess.run(
        [venv_bin("dbt"), "parse", "--no-partial-parse", "--quiet", "--project-dir", str(project),
         "--profiles-dir", str(project / "profiles" / "bigquery_compile")],
        cwd=project, env=env, capture_output=True, text=True, check=False,
    )
    assert parse.returncode == 0, parse.stdout + parse.stderr
    script = textwrap.dedent(
        f"""
        import os, pathlib, sys
        from dbt_metricflow.cli.cli_configuration import CLIConfiguration
        from peoplelens.catalog import manifest_guard
        project = pathlib.Path({str(project)!r})
        cfg = CLIConfiguration()
        cfg.setup(dbt_profiles_path=project / "profiles" / "duckdb", dbt_project_path=project, configure_file_logging=False)
        a = cfg.dbt_artifacts
        rels = [(sm.name, sm.node_relation.database) for sm in a.semantic_manifest.semantic_models]
        try:
            manifest_guard.check("duckdb", a.adapter.type(), rels)
        except manifest_guard.ManifestGuardError as exc:
            print("REJECTED", exc)
            sys.exit(0)
        print("ACCEPTED")
        sys.exit(1)
        """
    )
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "peoplelens-project-id" in proc.stdout
