"""Direct text-to-SQL baseline (eval harness only; there is no raw SQL path for users).

Same LLM and as-of date as the planner. The prompt carries the DDL and column
descriptions of the same semantic views, natural-language metric definitions,
and few-shot SQL from the dev set. Output is one SQL statement, guarded with
sqlglot (a single SELECT over the semantic views, no DDL or DML, no file or
table functions, no SELECT *), capped at 1,000 rows, and executed on a
read-only DuckDB connection (locally) or as the peoplelens-eval service account
(production). No suppression is applied: that is the point of the comparison.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

import duckdb
import sqlglot
import yaml
from sqlglot import exp

from peoplelens.eval.templates import TEMPLATE_DIR
from peoplelens.policy import load

ALLOWED_TABLES = {"sem_workforce_monthly", "sem_requisitions", "sem_offers"}
ROW_CAP = 1000
SQL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"sql": {"type": "string"}},
    "required": ["sql"],
}

DDL = """\
-- One row per employee per month in which the employee was employed at any point (2023-01 to 2025-12).
CREATE VIEW sem_workforce_monthly (
  workforce_key VARCHAR,         -- opaque row key
  month_start DATE,              -- first day of the month
  department VARCHAR,            -- Engineering, Sales, Customer Success, Marketing, Finance, People, Operations, Legal
  location VARCHAR,              -- San Jose, New York, Austin, London, Bengaluru
  job_level VARCHAR,             -- L1 .. L6
  employment_type VARCHAR,       -- full_time, part_time
  tenure_band VARCHAR,           -- <1y, 1-2y, 2-5y, 5y+ (at month end)
  is_active_eom INTEGER,         -- 1 if employed at the month end
  fte_eom DOUBLE,                -- 1.0, 0.8 or 0.5
  is_termination INTEGER,        -- 1 if the employee left during this month
  is_voluntary_termination INTEGER,
  base_salary_eom BIGINT         -- annual base salary in USD
);
-- Filled requisitions only.
CREATE VIEW sem_requisitions (
  requisition_id VARCHAR, department VARCHAR, location VARCHAR, job_level VARCHAR,
  filled_date DATE,              -- the day the requisition was filled
  days_to_fill INTEGER           -- days from opened to filled
);
-- Offers extended.
CREATE VIEW sem_offers (
  offer_id VARCHAR, department VARCHAR, location VARCHAR, job_level VARCHAR,
  offer_date DATE,
  is_accepted INTEGER            -- 1 if the candidate accepted
);"""

RULES = """\
Metric definitions:
- headcount: SUM(is_active_eom) at the last month_start in the period (point in time).
- fte: SUM(fte_eom) of rows with is_active_eom = 1 at the last month_start in the period.
- turnover_rate: SUM(is_termination) / (SUM(is_active_eom) / COUNT(DISTINCT month_start)) over the period. Not annualized.
- offer_acceptance_rate: SUM(is_accepted) / COUNT(*) of offers by offer_date in the period.
- time_to_fill_days: AVG(days_to_fill) of requisitions by filled_date in the period.
- compensation_summary: AVG and MEDIAN of base_salary_eom of rows with is_active_eom = 1 at the last month_start; return avg_base_salary and median_base_salary.
Rules: today is 2025-12-31. With no period, point-in-time metrics use December 2025 and rates use 2025.
For a breakdown by quarter, month or year, return the bucket start date (DATE_TRUNC) as a column.
Return one DuckDB SELECT statement as JSON {"sql": "..."}."""


@dataclass
class BaselineResult:
    sql: str | None = None
    guarded_sql: str | None = None
    guard_error: str | None = None
    exec_error: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    llm_ms: float = 0.0
    exec_ms: float = 0.0
    usage: dict = field(default_factory=dict)


class GuardError(ValueError):
    pass


def fewshot_sql() -> list[dict]:
    raw = yaml.safe_load((TEMPLATE_DIR / "dev.yaml").read_text(encoding="utf-8"))
    return raw.get("baseline_sql") or []


def dialect_sql(sql: str, dialect: str) -> str:
    """Few-shot SQL is written for DuckDB; transpile it for BigQuery (MEDIAN becomes APPROX_QUANTILES)."""
    if dialect == "duckdb":
        return " ".join(sql.split())
    out = sqlglot.transpile(sql, read="duckdb", write="bigquery")[0]
    # BigQuery semantic views carry TIMESTAMP time columns, and BigQuery has no MEDIAN aggregate.
    out = re.sub(r"CAST\('(\d{4}-\d{2}-\d{2})' AS DATE\)", r"TIMESTAMP('\1')", out)
    out = re.sub(r"MEDIAN\((.+?)\) AS", r"APPROX_QUANTILES(\1, 2)[OFFSET(1)] AS", out)
    return out


def build_messages(question: str, dialect: str = "duckdb") -> list[dict]:
    policy = load()
    defs = "\n".join(f"- {m['id']}: {m['definition']}" for m in policy.metrics.values())
    name = "DuckDB" if dialect == "duckdb" else "BigQuery standard"
    rules = RULES.replace("DuckDB SELECT", f"{name} SELECT")
    ddl = DDL
    if dialect == "bigquery":
        for col in ("month_start", "filled_date", "offer_date"):
            ddl = re.sub(rf"({col}) DATE", r"\1 TIMESTAMP", ddl)
        rules += " Use APPROX_QUANTILES(x, 2)[OFFSET(1)] for a median; time columns are TIMESTAMP."
    system = f"You write {name} SQL for workforce analytics questions.\n\n{ddl}\n\nGoverned metrics:\n{defs}\n\n{rules}"
    messages = [{"role": "system", "content": system}]
    for ex in fewshot_sql():
        messages.append({"role": "user", "content": ex["question"]})
        messages.append({"role": "assistant", "content": json.dumps({"sql": dialect_sql(ex["sql"], dialect)})})
    messages.append({"role": "user", "content": question})
    return messages


FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Merge, exp.Command,
    exp.TruncateTable, exp.Copy, exp.Set, exp.Attach, exp.Detach, exp.Pragma, exp.Use, exp.Grant,
)


def guard(sql: str, dialect: str = "duckdb") -> str:
    """Return the capped SQL, or raise GuardError."""
    if not sql or not sql.strip():
        raise GuardError("empty")
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise GuardError(f"parse error: {str(exc)[:80]}") from exc
    if len(statements) != 1:
        raise GuardError("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise GuardError(f"only SELECT is allowed, got {type(tree).__name__}")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN):
            raise GuardError(f"{type(node).__name__} is not allowed")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name:
            raise GuardError("table functions are not allowed")
        if name not in ALLOWED_TABLES and name not in ctes:
            raise GuardError(f"table {name} is not allowed")
        if table.db and table.db.lower() not in ("semantic", "peoplelens_semantic"):
            raise GuardError("only the semantic schema is allowed")
    for fn in tree.find_all(exp.Anonymous):
        if fn.name.lower().startswith(("read_", "glob", "scan_", "sniff_", "query", "duckdb_")):
            raise GuardError(f"function {fn.name} is not allowed")
    for select in tree.find_all(exp.Select):
        for proj in select.expressions:
            if isinstance(proj, exp.Star) or (isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)):
                raise GuardError("SELECT * is not allowed")
    inner = tree.sql(dialect=dialect)
    return f"SELECT * FROM ({inner}) AS baseline_q LIMIT {ROW_CAP}"


def parse_sql_output(text: str) -> str | None:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    sql = obj.get("sql") if isinstance(obj, dict) else None
    return sql if isinstance(sql, str) else None


class LocalExecutor:
    """Read-only DuckDB connection (never the sidecar)."""

    kind = "duckdb-local"
    dialect = "duckdb"

    def __init__(self, db_path, timeout_s: float = 10.0):
        self.con = duckdb.connect(str(db_path), read_only=True)
        self.con.execute("SET search_path = 'peoplelens.semantic'")
        self.timeout_s = timeout_s

    def run(self, sql: str) -> tuple[list[str], list[list]]:
        timer = threading.Timer(self.timeout_s, self.con.interrupt)
        timer.start()
        try:
            cur = self.con.execute(sql)
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        finally:
            timer.cancel()
        return columns, rows

    def close(self) -> None:
        self.con.close()


def run_baseline(question: str, llm, executor) -> BaselineResult:
    out = BaselineResult()
    try:
        text, out.llm_ms, out.usage = llm.complete(build_messages(question, executor.dialect), SQL_SCHEMA)
    except Exception as exc:  # noqa: BLE001 - any LLM failure is a baseline miss
        out.exec_error = f"llm: {type(exc).__name__}"
        return out
    out.sql = parse_sql_output(text)
    if out.sql is None:
        out.guard_error = "no sql in output"
        return out
    try:
        out.guarded_sql = guard(out.sql, executor.dialect)
    except GuardError as exc:
        out.guard_error = str(exc)
        return out
    started = time.perf_counter()
    try:
        out.columns, out.rows = executor.run(out.guarded_sql)
    except Exception as exc:  # noqa: BLE001
        out.exec_error = type(exc).__name__
    out.exec_ms = (time.perf_counter() - started) * 1000
    return out
