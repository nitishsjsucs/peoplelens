"""BigQuery catalog rendered offline from a BigQuery-parsed manifest (SPEC.md section 8)."""

from __future__ import annotations

import json
import re

from peoplelens.paths import GENERATED_DIR

CATALOG = json.loads((GENERATED_DIR / "catalog.bigquery.json").read_text())
ENTRIES = CATALOG["entries"]


def test_header_marks_placeholder_project():
    assert CATALOG["header"]["dialect"] == "bigquery"
    assert CATALOG["header"]["project_placeholder"] == "peoplelens-project-id"


def test_no_datetime_casts():
    # TIMESTAMP vs DATETIME mismatches only surface in a BigQuery dry run; the
    # time columns are TIMESTAMP (pl_time), so no rendered SQL may cast to DATETIME.
    offenders = [k for k, e in ENTRIES.items() if "AS DATETIME" in e["sql"].upper()]
    assert offenders == []


def test_bigquery_parameter_syntax():
    for key, e in ENTRIES.items():
        sql = e["sql"]
        assert "$" not in sql, key
        assert "list_contains" not in sql, key
        assert "IN UNNEST(@scope_departments)" in sql, key
        assert "BETWEEN @start_ts AND @end_ts" in sql, key
        assert all(p["type"] in ("TIMESTAMP", "ARRAY<STRING>") for p in e["params"])


def test_reads_only_the_semantic_dataset_of_the_placeholder_project():
    for key, e in ENTRIES.items():
        relations = set(re.findall(r"`([^`]+)`\.`([^`]+)`\.`([^`]+)`", e["sql"]))
        assert relations, key
        for project, dataset, table in relations:
            assert project == "peoplelens-project-id", key
            assert dataset == "peoplelens_semantic", key
            assert table in ("sem_workforce_monthly", "sem_requisitions", "sem_offers", "metricflow_time_spine"), key


def test_bigquery_time_and_percentile_rendering():
    wf = ENTRIES["headcount||month"]["sql"]
    assert "TIMESTAMP_TRUNC(month_start, month)" in wf
    comp = ENTRIES["compensation_summary|location|year"]["sql"]
    assert "APPROX_QUANTILES(" in comp and "[OFFSET(1)]" in comp
    loc = ENTRIES["headcount|location|none"]["sql"]
    assert "IN UNNEST(@f_location)" in loc


def test_turnover_divides_safely():
    for key, e in ENTRIES.items():
        if e["metric"] == "turnover_rate":
            assert "terminations / NULLIF(avg_headcount, 0)" in e["sql"], key
