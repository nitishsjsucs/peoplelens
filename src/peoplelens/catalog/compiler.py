"""`pl catalog build`: render every governed query shape with MetricFlow, ahead of time.

MetricFlow is Python and cannot run inside a Worker, so the Worker executes SQL
that MetricFlow generated at build time from the governed semantic layer. Each
dialect compiles in its own subprocess (BigQuery first, then DuckDB, so
target/ ends in the DuckDB state that `mf` dev commands expect):

  1. `pl dbt --dialect <d> parse --no-partial-parse`
  2. CLIConfiguration().setup(<dialect profiles dir>, warehouse/dbt)
  3. manifest guard (adapter type and relation database)
  4. cfg.mf.explain(MetricFlowQueryRequest.create(...)).sql_statement.sql per shape

`--check` does the same work and diffs against the committed files.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from peoplelens.catalog import manifest_guard
from peoplelens.catalog.dims import mf_dimension, mf_time, query_metrics
from peoplelens.catalog.keys import catalog_key, shapes
from peoplelens.catalog.params import param_contract, where_filters
from peoplelens.paths import DBT_PROFILES_ROOT, DBT_PROJECT_DIR, DUCKDB_PATH, EVAL_DIR, GENERATED_DIR
from peoplelens.policy import Policy, load

COMPILE_ORDER = ("bigquery_compile", "duckdb")
SQL_DIALECT = {"bigquery_compile": "bigquery", "duckdb": "duckdb"}
BIGQUERY_PROJECT_PLACEHOLDER = "peoplelens-project-id"
FEWSHOT_COUNT = 12


class CompileError(RuntimeError):
    pass


def strip_comment_lines(sql: str) -> str:
    lines = [line.rstrip() for line in sql.splitlines()]
    return "\n".join(line for line in lines if line.strip() and not line.strip().startswith("--")) + "\n"


def dump_json(obj: object) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- child process


def compile_dialect(dialect: str, policy: Policy) -> dict:
    """Parse, set up MetricFlow, guard, and render all shapes for one dialect."""
    from peoplelens.dbt_runner import dbt_env, run_dbt

    parsed = run_dbt(dialect, ["parse", "--no-partial-parse", "--quiet"], capture=True)
    if parsed.returncode != 0:
        raise CompileError(f"dbt parse failed for {dialect}:\n{parsed.stdout}\n{parsed.stderr}")

    # The DuckDB profile reads PL_DUCKDB_PATH; never let a stray DBT_PROFILES_DIR win.
    for key, value in dbt_env(dialect).items():
        os.environ[key] = value
    os.environ.pop("DBT_PROFILES_DIR", None)

    from dbt_metricflow.cli.cli_configuration import CLIConfiguration
    from metricflow.engine.metricflow_engine import MetricFlowQueryRequest

    cfg = CLIConfiguration()
    cfg.setup(
        dbt_profiles_path=DBT_PROFILES_ROOT / dialect,
        dbt_project_path=DBT_PROJECT_DIR,
        configure_file_logging=False,
    )
    artifacts = cfg.dbt_artifacts
    relations = [(sm.name, sm.node_relation.database) for sm in artifacts.semantic_manifest.semantic_models]
    manifest_guard.check(dialect, artifacts.adapter.type(), relations)

    sql_dialect = SQL_DIALECT[dialect]
    entries: dict[str, dict] = {}
    for metric_id, group_by, grain in shapes(policy):
        dim_cols = {d: mf_dimension(policy, metric_id, d) for d in group_by}
        time_col = mf_time(grain)
        group_by_names = list(dim_cols.values()) + ([time_col] if time_col else [])
        mf_metrics = query_metrics(policy, metric_id)
        request = MetricFlowQueryRequest.create(
            metric_names=mf_metrics,
            group_by_names=group_by_names or None,
            where_constraints=where_filters(policy, metric_id, sql_dialect),
            order_by_names=group_by_names or None,
        )
        sql = strip_comment_lines(cfg.mf.explain(request).sql_statement.sql)
        key = catalog_key(metric_id, group_by, grain)
        m = policy.metrics[metric_id]
        entries[key] = {
            "key": key,
            "metric": metric_id,
            "group_by": list(group_by),
            "grain": grain,
            "mf_metrics": mf_metrics,
            "columns": {
                "dimensions": dim_cols,
                "time": time_col,
                "values": list(m["mf_metrics"]),
                "cohort": m["cohort_metric"],
            },
            "params": param_contract(policy, metric_id, sql_dialect),
            "sql": sql,
            "sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "row_bound": {"dims": list(group_by), "grain": grain},
        }
    return {
        "header": {
            "dialect": sql_dialect,
            "entries": len(entries),
            "metricflow": version("metricflow"),
            "dbt_metricflow": version("dbt-metricflow"),
            "dbt_core": version("dbt-core"),
            "manifest_sha256": manifest_guard.manifest_sha256(DBT_PROJECT_DIR),
            "project_placeholder": BIGQUERY_PROJECT_PLACEHOLDER if sql_dialect == "bigquery" else None,
        },
        "entries": entries,
    }


def _child_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m peoplelens.catalog.compiler")
    parser.add_argument("--dialect", required=True, choices=COMPILE_ORDER)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        catalog = compile_dialect(args.dialect, load())
    except (CompileError, manifest_guard.ManifestGuardError) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    Path(args.out).write_text(dump_json(catalog), encoding="utf-8")
    return 0


# ---------------------------------------------------------------- parent process


def warehouse_lock_problem() -> str | None:
    """CLIConfiguration opens the DuckDB file read-write, so nothing else may hold it."""
    import duckdb

    if not DUCKDB_PATH.exists():
        return (
            f"DuckDB warehouse missing at {DUCKDB_PATH}. Build it first: "
            "`uv run pl dbt --dialect duckdb seed` and `uv run pl dbt --dialect duckdb build`."
        )
    try:
        con = duckdb.connect(str(DUCKDB_PATH))
        con.close()
    except duckdb.Error as exc:
        return (
            f"the DuckDB warehouse is in use by another process ({exc}). Stop the sidecar with "
            "`uv run pl dev down` before `pl catalog build`."
        )
    return None


def dimension_values(policy: Policy) -> dict[str, list[str]]:
    """Distinct non-PII labels of each approved dimension, from the semantic views (read-only)."""
    import duckdb

    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        found: dict[str, set[str]] = {}
        for view in ("sem_workforce_monthly", "sem_requisitions", "sem_offers"):
            cols = {r[0] for r in con.execute(
                "select column_name from information_schema.columns where table_schema = 'semantic' and table_name = ?",
                [view],
            ).fetchall()}
            for dim in policy.dimensions:
                if dim in cols:
                    rows = con.execute(f'select distinct "{dim}" from "peoplelens"."semantic"."{view}"').fetchall()
                    found.setdefault(dim, set()).update(r[0] for r in rows if r[0] is not None)
    finally:
        con.close()
    out: dict[str, list[str]] = {}
    for dim_id, dim in policy.dimensions.items():
        declared = list(dim["values"])
        if set(declared) != found.get(dim_id, set()):
            raise CompileError(
                f"warehouse values for {dim_id} {sorted(found.get(dim_id, set()))} differ from policy/dimensions.json {declared}"
            )
        out[dim_id] = declared
    return out


def fewshot_examples() -> list[dict]:
    """Few-shot planner examples, taken only from the dev set (never from eval items)."""
    path = EVAL_DIR / "dev_questions.jsonl"
    if not path.exists():
        return []
    items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    picked = [i for i in items if i.get("fewshot")]
    picked.sort(key=lambda i: i["id"])
    return [
        {"id": i["id"], "role": i["role"], "question": i["turns"][0]["text"], "output": i["expected_plan"]}
        for i in picked[:FEWSHOT_COUNT]
    ]


def render_outputs(catalogs: dict[str, dict], policy: Policy, values: dict[str, list[str]]) -> dict[str, str]:
    return {
        "catalog.bigquery.json": dump_json(catalogs["bigquery_compile"]),
        "catalog.duckdb.json": dump_json(catalogs["duckdb"]),
        "dimension_values.json": dump_json(values),
        "policy.json": dump_json(policy.merged()),
        "fewshot.json": dump_json(fewshot_examples()),
    }


def run_child(dialect: str, out: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "peoplelens.catalog.compiler", "--dialect", dialect, "--out", str(out)],
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise CompileError(f"catalog compile failed for {dialect} (exit {proc.returncode}):\n{proc.stderr[-4000:]}")


def build(check: bool = False, out_dir: Path = GENERATED_DIR) -> int:
    problem = warehouse_lock_problem()
    if problem:
        print(f"pl catalog build: {problem}", file=sys.stderr)
        return 2
    policy = load()
    try:
        values = dimension_values(policy)
        catalogs: dict[str, dict] = {}
        with tempfile.TemporaryDirectory(prefix="pl-catalog-") as tmp:
            for dialect in COMPILE_ORDER:
                out = Path(tmp) / f"{dialect}.json"
                run_child(dialect, out)
                catalogs[dialect] = json.loads(out.read_text(encoding="utf-8"))
                print(f"compiled {catalogs[dialect]['header']['entries']} entries for {dialect}")
    except (CompileError, manifest_guard.ManifestGuardError) as exc:
        print(f"pl catalog build: {exc}", file=sys.stderr)
        return 3
    outputs = render_outputs(catalogs, policy, values)
    if check:
        failed = False
        for name, text in outputs.items():
            path = out_dir / name
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != text:
                failed = True
                diff = list(difflib.unified_diff(current.splitlines(), text.splitlines(), f"committed/{name}", f"fresh/{name}", lineterm="", n=1))
                print(f"DRIFT {name}: {len(diff)} diff lines", file=sys.stderr)
                for line in diff[:40]:
                    print(line, file=sys.stderr)
        if failed:
            print("Run `uv run pl catalog build` and commit worker/src/generated/.", file=sys.stderr)
            return 1
        print(f"OK generated catalog files are fresh ({len(outputs)} files)")
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in outputs.items():
        (out_dir / name).write_text(text, encoding="utf-8")
    print(f"wrote {len(outputs)} files to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
