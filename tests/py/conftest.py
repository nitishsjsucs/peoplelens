"""Shared pytest fixtures.

`require_warehouse` skips warehouse-dependent tests on a laptop where the DuckDB
file has not been built yet, and fails them instead when CI or PL_STRICT is set,
so CI can never pass by skipping.
"""

from __future__ import annotations

import os

import pytest

from peoplelens.paths import DUCKDB_PATH


def _strict() -> bool:
    return bool(os.environ.get("CI")) or bool(os.environ.get("PL_STRICT"))


@pytest.fixture(scope="session")
def require_warehouse():
    if not DUCKDB_PATH.exists():
        msg = (
            f"DuckDB warehouse missing at {DUCKDB_PATH}. Build it with "
            "`uv run pl dbt --dialect duckdb seed` and `uv run pl dbt --dialect duckdb build`."
        )
        if _strict():
            pytest.fail(msg)
        pytest.skip(msg)
    return DUCKDB_PATH
