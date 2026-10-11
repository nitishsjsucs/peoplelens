"""`pl`: the developer CLI. Subcommands import their modules lazily."""

from __future__ import annotations

import argparse
import json
import sys


def _cmd_data_generate(args: argparse.Namespace) -> int:
    from peoplelens.paths import SEEDS_DIR, SHA256SUMS_PATH
    from peoplelens.synth.generator import render, sha256sums

    files = render()
    sums = sha256sums(files)
    if args.check:
        problems = []
        if not SHA256SUMS_PATH.exists() or SHA256SUMS_PATH.read_bytes() != sums:
            problems.append(f"{SHA256SUMS_PATH.name} differs from a fresh generation")
        for name, data in files.items():
            path = SEEDS_DIR / name
            if not path.exists() or path.read_bytes() != data:
                problems.append(f"{name} differs from a fresh generation")
        if problems:
            for p in problems:
                print(f"FAIL {p}", file=sys.stderr)
            print("Run `uv run pl data generate` and commit the result if the change is deliberate.", file=sys.stderr)
            return 1
        print(f"OK seeds are byte-identical to a fresh generation ({len(files)} files)")
        return 0
    SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (SEEDS_DIR / name).write_bytes(data)
    SHA256SUMS_PATH.write_bytes(sums)
    print(f"wrote {len(files)} seed files and SHA256SUMS to {SEEDS_DIR}")
    return 0


def _cmd_data_stats(args: argparse.Namespace) -> int:
    from peoplelens.synth.stats import compute

    print(json.dumps(compute(), indent=2))
    return 0


def _cmd_dbt(args: argparse.Namespace) -> int:
    from peoplelens.dbt_runner import DbtRunnerError, run_dbt

    rest = list(args.dbt_args)
    if rest and rest[0] == "--":
        rest = rest[1:]
    try:
        return run_dbt(args.dialect, rest).returncode
    except DbtRunnerError as exc:
        print(f"pl dbt: {exc}", file=sys.stderr)
        return 2


def _cmd_mf_validate(args: argparse.Namespace) -> int:
    from peoplelens.dbt_runner import run_mf_validate

    return run_mf_validate()


def _cmd_catalog_build(args: argparse.Namespace) -> int:
    from peoplelens.catalog.compiler import build

    return build(check=args.check)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pl", description="PeopleLens developer CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    data = sub.add_parser("data", help="synthetic seed data")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    gen = data_sub.add_parser("generate", help="write seed CSVs and SHA256SUMS")
    gen.add_argument("--check", action="store_true", help="regenerate in memory and compare, write nothing")
    gen.set_defaults(func=_cmd_data_generate)
    stats = data_sub.add_parser("stats", help="print exact counts computed from the seed CSVs")
    stats.set_defaults(func=_cmd_data_stats)

    dbt = sub.add_parser(
        "dbt",
        help="run dbt with the dialect's profiles dir and PL_DUCKDB_PATH (the only supported way to run dbt)",
    )
    dbt.add_argument("--dialect", required=True, choices=["duckdb", "bigquery_compile", "bigquery"])
    dbt.add_argument("dbt_args", nargs=argparse.REMAINDER, help="arguments passed to dbt, for example: build")
    dbt.set_defaults(func=_cmd_dbt)

    mf = sub.add_parser("mf", help="MetricFlow helpers")
    mf_sub = mf.add_subparsers(dest="mf_command", required=True)
    mfv = mf_sub.add_parser("validate", help="mf validate-configs --skip-dw after a DuckDB dbt parse")
    mfv.set_defaults(func=_cmd_mf_validate)

    catalog = sub.add_parser("catalog", help="compile-ahead MetricFlow query catalog")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    cbuild = catalog_sub.add_parser("build", help="compile both dialects (BigQuery first, then DuckDB)")
    cbuild.add_argument("--check", action="store_true", help="compile and diff against worker/src/generated, write nothing")
    cbuild.set_defaults(func=_cmd_catalog_build)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
