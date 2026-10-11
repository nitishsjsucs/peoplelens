"""Repository paths. Every path in the Python side is derived from here."""

from __future__ import annotations

import os
from pathlib import Path


def _find_root() -> Path:
    override = os.environ.get("PL_REPO_ROOT")
    if override:
        return Path(override).resolve()
    # src/peoplelens/paths.py -> repo root is two levels above the package dir.
    return Path(__file__).resolve().parents[2]


REPO_ROOT = _find_root()
POLICY_DIR = REPO_ROOT / "policy"
DBT_PROJECT_DIR = REPO_ROOT / "warehouse" / "dbt"
DBT_PROFILES_ROOT = DBT_PROJECT_DIR / "profiles"
SEEDS_DIR = DBT_PROJECT_DIR / "seeds"
SHA256SUMS_PATH = SEEDS_DIR / "SHA256SUMS"
DEV_DIR = REPO_ROOT / ".dev"
WAREHOUSE_DIR = DEV_DIR / "warehouse"
DUCKDB_PATH = WAREHOUSE_DIR / "peoplelens.duckdb"
# local audit trail: staged NDJSON objects (the stand-in for the R2 bucket) and the
# pyiceberg SqlCatalog (the stand-in for Basin Catalog)
AUDIT_STAGING_DIR = DEV_DIR / "audit-staging"
ICEBERG_DIR = DEV_DIR / "iceberg"
WORKER_DIR = REPO_ROOT / "worker"
GENERATED_DIR = WORKER_DIR / "src" / "generated"
EVAL_DIR = REPO_ROOT / "eval"
SECRETS_DIR = REPO_ROOT / ".secrets"
