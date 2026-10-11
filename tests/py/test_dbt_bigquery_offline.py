"""The production dbt project (dbt-bigquery, `pl dbt --dialect bigquery`) compiles offline.

`dbt compile --no-populate-cache` renders every BigQuery model without running a
query. dbt-bigquery still builds a client, so the test points
GOOGLE_APPLICATION_CREDENTIALS at a throwaway service-account key generated here
(valid nowhere) and CLOUDSDK_CONFIG at an empty directory: no real credential can
be picked up, and nothing is sent anywhere.

Checked: every model compiles and parses as BigQuery SQL; the semantic views are
authorized views over peoplelens_core (grant_access_to) and read nothing else;
policy tags stay off by default and, with the phase C vars, land on exactly the
eight documented columns.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest
import sqlglot
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from peoplelens.dbt_runner import dbt_command, dbt_env

PROJECT = "peoplelens-ci-check"
TAG = "projects/" + PROJECT + "/locations/us/taxonomies/1/policyTags/{}"
PHASE_C_VARS = {
    "apply_policy_tags": True,
    "policy_tag_pii_high": TAG.format(11),
    "policy_tag_protected_class": TAG.format(12),
    "policy_tag_compensation": TAG.format(13),
}
EXPECTED_TAGS = {
    ("raw_employees", "full_name"): PHASE_C_VARS["policy_tag_pii_high"],
    ("raw_employees", "work_email"): PHASE_C_VARS["policy_tag_pii_high"],
    ("raw_candidates", "full_name"): PHASE_C_VARS["policy_tag_pii_high"],
    ("raw_candidates", "email"): PHASE_C_VARS["policy_tag_pii_high"],
    ("raw_employees", "date_of_birth"): PHASE_C_VARS["policy_tag_protected_class"],
    ("raw_employees", "gender"): PHASE_C_VARS["policy_tag_protected_class"],
    ("raw_compensation_history", "base_salary_usd"): PHASE_C_VARS["policy_tag_compensation"],
    ("fct_workforce_monthly", "base_salary_eom"): PHASE_C_VARS["policy_tag_compensation"],
}
RELATION = re.compile(r"`([^`]+)`\.`([^`]+)`\.`([^`]+)`")


def _fake_key(path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    path.write_text(json.dumps({
        "type": "service_account",
        "project_id": PROJECT,
        "private_key_id": "0" * 40,
        "private_key": pem,
        "client_email": f"offline-check@{PROJECT}.iam.gserviceaccount.com",
        "client_id": "0",
        "token_uri": "https://oauth2.googleapis.com/token",
    }))


def _compile(tmp_path, name: str, dbt_vars: dict | None = None) -> tuple[dict, object]:
    key = tmp_path / "throwaway-sa.json"
    if not key.exists():
        _fake_key(key)
    gcloud_cfg = tmp_path / "gcloud-empty"
    gcloud_cfg.mkdir(exist_ok=True)
    target = tmp_path / f"target-{name}"
    args = ["compile", "--no-populate-cache", "--target-path", str(target), "--log-path", str(tmp_path / f"logs-{name}")]
    if dbt_vars:
        args += ["--vars", json.dumps(dbt_vars)]
    env = dbt_env("bigquery", {"PATH": os.environ["PATH"], "GCP_PROJECT_ID": PROJECT,
                               "GOOGLE_APPLICATION_CREDENTIALS": str(key), "CLOUDSDK_CONFIG": str(gcloud_cfg),
                               "HOME": str(tmp_path)})
    proc = subprocess.run(dbt_command("bigquery", args), env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    return json.loads((target / "manifest.json").read_text()), target


@pytest.fixture(scope="module")
def default_build(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("bq-default")
    return _compile(tmp, "default")


def _models(manifest: dict) -> dict[str, dict]:
    return {n["name"]: n for n in manifest["nodes"].values() if n["resource_type"] == "model"}


def test_every_model_compiles_to_bigquery_sql(default_build):
    manifest, _ = default_build
    models = _models(manifest)
    assert len(models) == 14
    for name, node in models.items():
        sql = node["compiled_code"]
        assert "::" not in sql, f"{name}: DuckDB-style cast in BigQuery SQL"
        assert "list_contains" not in sql and "strftime" not in sql, name
        sqlglot.parse(sql, read="bigquery", error_level=sqlglot.ErrorLevel.RAISE)
        for project, dataset, _table in RELATION.findall(sql):
            assert project == PROJECT, f"{name} reads project {project}"
            assert dataset in {"peoplelens_raw", "peoplelens_staging", "peoplelens_core", "peoplelens_semantic"}, name


def test_semantic_views_are_authorized_views_over_core_only(default_build):
    manifest, _ = default_build
    models = _models(manifest)
    for name in ("sem_workforce_monthly", "sem_requisitions", "sem_offers"):
        node = models[name]
        assert node["schema"] == "peoplelens_semantic"
        assert node["config"]["materialized"] == "view"
        assert node["config"]["grant_access_to"] == [{"project": PROJECT, "dataset": "peoplelens_core"}]
        datasets = {d for _p, d, _t in RELATION.findall(node["compiled_code"])}
        assert datasets == {"peoplelens_core"}, f"{name} reads {datasets}"
    spine = models["metricflow_time_spine"]
    assert spine["schema"] == "peoplelens_semantic" and spine["config"]["materialized"] == "table"
    for node in models.values():
        if node["schema"] != "peoplelens_semantic":
            assert not node["config"].get("grant_access_to"), node["name"]


def test_time_columns_are_timestamps_on_bigquery(default_build):
    """MetricFlow renders TIMESTAMP_TRUNC for BigQuery, so the semantic time columns are TIMESTAMP."""
    manifest, _ = default_build
    models = _models(manifest)
    assert "cast(month_start as timestamp)" in models["fct_workforce_monthly"]["compiled_code"]
    assert "cast(filled_date as timestamp)" in models["fct_requisitions"]["compiled_code"]
    assert "cast(o.offer_date as timestamp)" in models["fct_offers"]["compiled_code"]
    assert "cast(date_day as timestamp)" in models["metricflow_time_spine"]["compiled_code"]


def test_policy_tags_are_off_by_default(default_build):
    manifest, _ = default_build
    for node in manifest["nodes"].values():
        if node["resource_type"] in ("model", "seed"):
            assert not (node["config"].get("persist_docs") or {}).get("columns"), node["name"]


def test_phase_c_vars_tag_exactly_the_eight_columns(tmp_path):
    manifest, _ = _compile(tmp_path, "phase-c", PHASE_C_VARS)
    tagged = {}
    for node in manifest["nodes"].values():
        if node["resource_type"] not in ("model", "seed"):
            continue
        tags_here = {c: v["policy_tags"] for c, v in node["columns"].items() if any(v.get("policy_tags") or [])}
        if tags_here:
            assert node["config"]["persist_docs"]["columns"] is True, node["name"]
        for column, tags in tags_here.items():
            assert len(tags) == 1, (node["name"], column, tags)
            tagged[(node["name"], column)] = tags[0]
    assert tagged == EXPECTED_TAGS
