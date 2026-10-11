"""`pl dbt` wrapper contract (SPEC.md sections 2, 8 and 16)."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from peoplelens import dbt_runner
from peoplelens.paths import DBT_PROFILES_ROOT, DBT_PROJECT_DIR, DUCKDB_PATH


@pytest.mark.parametrize("dialect", dbt_runner.DIALECTS)
def test_command_pins_project_and_profiles_dir(dialect):
    cmd = dbt_runner.dbt_command(dialect, ["build", "--select", "core"])
    assert cmd[1:4] == ["build", "--select", "core"]
    assert cmd[cmd.index("--project-dir") + 1] == str(DBT_PROJECT_DIR)
    assert cmd[cmd.index("--profiles-dir") + 1] == str(DBT_PROFILES_ROOT / dialect)


def test_unknown_dialect_and_empty_args_are_rejected():
    with pytest.raises(dbt_runner.DbtRunnerError):
        dbt_runner.dbt_command("postgres", ["build"])
    with pytest.raises(dbt_runner.DbtRunnerError):
        dbt_runner.dbt_command("duckdb", [])


def test_env_sets_absolute_duckdb_path_and_drops_target_overrides():
    env = dbt_runner.dbt_env("duckdb", {"DBT_TARGET": "x", "DBT_PROFILES_DIR": "/elsewhere"})
    assert env["PL_DUCKDB_PATH"] == str(DUCKDB_PATH.resolve())
    assert env["PL_DUCKDB_PATH"].endswith("/.dev/warehouse/peoplelens.duckdb")
    assert "DBT_TARGET" not in env and "DBT_PROFILES_DIR" not in env


def test_bigquery_requires_valid_project_id():
    with pytest.raises(dbt_runner.DbtRunnerError, match="GCP_PROJECT_ID is not set"):
        dbt_runner.dbt_env("bigquery", {})
    with pytest.raises(dbt_runner.DbtRunnerError, match="not a valid"):
        dbt_runner.dbt_env("bigquery", {"GCP_PROJECT_ID": "Bad_Project"})
    env = dbt_runner.dbt_env("bigquery", {"GCP_PROJECT_ID": "peoplelens-abc123"})
    assert env["GCP_PROJECT_ID"] == "peoplelens-abc123"
    # The offline compile profile needs no project or credentials.
    dbt_runner.dbt_env("bigquery_compile", {})


def test_run_creates_warehouse_dir(tmp_path, monkeypatch):
    target = tmp_path / ".dev" / "warehouse"
    monkeypatch.setattr(dbt_runner, "WAREHOUSE_DIR", target)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(dbt_runner.subprocess, "run", fake_run)
    dbt_runner.run_dbt("duckdb", ["seed"])
    assert target.is_dir()
    cmd, kwargs = calls[0]
    assert cmd[1] == "seed"
    assert kwargs["cwd"] == str(DBT_PROJECT_DIR)
    assert kwargs["env"]["PL_DUCKDB_PATH"] == str(DUCKDB_PATH.resolve())


@pytest.mark.parametrize("dialect", dbt_runner.DIALECTS)
def test_each_profiles_dir_has_its_own_default_target(dialect):
    profiles = yaml.safe_load((DBT_PROFILES_ROOT / dialect / "profiles.yml").read_text())
    profile = profiles["peoplelens"]
    assert profile["target"] == dialect
    assert list(profile["outputs"]) == [dialect]


def test_duckdb_profile_path_has_no_default():
    profile = yaml.safe_load((DBT_PROFILES_ROOT / "duckdb" / "profiles.yml").read_text())
    assert profile["peoplelens"]["outputs"]["duckdb"]["path"] == "{{ env_var('PL_DUCKDB_PATH') }}"


def test_bigquery_compile_profile_uses_placeholder_project():
    profile = yaml.safe_load((DBT_PROFILES_ROOT / "bigquery_compile" / "profiles.yml").read_text())
    out = profile["peoplelens"]["outputs"]["bigquery_compile"]
    assert out["project"] == "peoplelens-project-id"
    assert out["method"] == "oauth"


def test_bare_dbt_fails_fast_without_the_wrapper(tmp_path):
    env = dbt_runner.dbt_env("duckdb", {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    env.pop("PL_DUCKDB_PATH")
    proc = subprocess.run(
        [dbt_runner.venv_bin("dbt"), "debug", "--config-dir", "--project-dir", str(DBT_PROJECT_DIR),
         "--profiles-dir", str(DBT_PROFILES_ROOT / "duckdb")],
        env=env, capture_output=True, text=True, check=False,
    )
    # `debug --config-dir` only prints the profiles dir; a real command must fail on the missing var.
    proc = subprocess.run(
        [dbt_runner.venv_bin("dbt"), "parse", "--project-dir", str(DBT_PROJECT_DIR),
         "--profiles-dir", str(DBT_PROFILES_ROOT / "duckdb"), "--target-path", str(tmp_path / "target"),
         "--log-path", str(tmp_path / "logs")],
        env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0
    assert "PL_DUCKDB_PATH" in proc.stdout + proc.stderr
