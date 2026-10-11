"""Text-to-SQL baseline: the sqlglot guard and the dev few-shot SQL."""

from __future__ import annotations

import duckdb
import pytest

from peoplelens.eval.baseline import GuardError, LocalExecutor, build_messages, fewshot_sql, guard
from peoplelens.eval.compare import compare_baseline
from peoplelens.eval.generate import QUESTIONS_PATH, load_items
from peoplelens.eval.gold import gold_for_spec

REJECT = [
    "DROP TABLE sem_offers",
    "DELETE FROM sem_offers",
    "UPDATE sem_workforce_monthly SET fte_eom = 1",
    "INSERT INTO sem_offers VALUES (1)",
    "CREATE TABLE x AS SELECT 1",
    "SELECT 1; SELECT 2",
    "SELECT department FROM sem_workforce_monthly; DROP TABLE sem_offers",
    "SELECT * FROM sem_offers",
    "SELECT o.* FROM sem_offers o",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT full_name FROM raw.raw_employees",
    "SELECT employee_id FROM core.fct_workforce_monthly",
    "ATTACH 'x.db'",
    "PRAGMA database_list",
    "SET threads = 1",
    "COPY sem_offers TO 'out.csv'",
    "",
    "not sql at all ((",
]


@pytest.mark.parametrize("sql", REJECT)
def test_guard_rejects(sql):
    with pytest.raises(GuardError):
        guard(sql)


def test_guard_accepts_reads_and_caps_rows():
    out = guard("WITH t AS (SELECT department, COUNT(*) AS n FROM sem_workforce_monthly GROUP BY 1) SELECT department, n FROM t")
    assert out.endswith("LIMIT 1000")
    assert guard("SELECT COUNT(*) FROM semantic.sem_offers").startswith("SELECT * FROM (")


def test_prompt_has_schema_and_dev_fewshot_only():
    messages = build_messages("What is headcount?")
    system = messages[0]["content"]
    assert "CREATE VIEW sem_workforce_monthly" in system and "turnover_rate" in system
    eval_texts = {t["text"] for i in load_items(QUESTIONS_PATH) for t in i["turns"]}
    for m in messages[1:-1]:
        assert m["content"] not in eval_texts
    assert len(messages) == 2 + 2 * len(fewshot_sql())


def test_read_only_executor(require_warehouse):
    ex = LocalExecutor(require_warehouse)
    try:
        with pytest.raises(duckdb.Error):
            ex.run("CREATE TABLE semantic.x AS SELECT 1 AS a")
    finally:
        ex.close()


@pytest.mark.parametrize("example", fewshot_sql(), ids=lambda e: e["question"])
def test_dev_fewshot_sql_matches_reference(require_warehouse, example):
    ex = LocalExecutor(require_warehouse)
    try:
        columns, rows = ex.run(guard(example["sql"]))
    finally:
        ex.close()
    ok, detail = compare_baseline(columns, rows, gold_for_spec(example["spec"]))
    assert ok, detail
