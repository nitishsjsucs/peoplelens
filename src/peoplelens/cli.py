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


def _cmd_warehouse_serve(args: argparse.Namespace) -> int:
    import signal

    from peoplelens.warehouse.server import serve

    def _stop(signum, frame):  # SIGTERM from `pl dev down` stops serve_forever cleanly
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        serve(port=args.port)
    except ValueError as exc:
        print(f"pl warehouse serve: {exc}", file=sys.stderr)
        return 2
    return 0


def _cmd_dev(args: argparse.Namespace) -> int:
    from peoplelens.dev import llm, orchestrator
    from peoplelens.dev.keys import ensure_keys

    if args.dev_command == "up":
        return orchestrator.up(args.llm)
    if args.dev_command == "down":
        return orchestrator.down()
    if args.dev_command == "keys":
        path, _ = ensure_keys()
        print(f"local RS256 key: {path}")
        return 0
    if args.dev_command == "llm-up":
        return llm.up()
    if args.dev_command == "llm-down":
        return llm.down()
    return 2


def _local_client(role: str):
    from peoplelens.dev.client import WorkerClient
    from peoplelens.dev.orchestrator import BASE_URL
    from peoplelens.dev.tokens import mint

    return WorkerClient(BASE_URL, {"Cf-Access-Jwt-Assertion": mint(role=role)})


def _cmd_token(args: argparse.Namespace) -> int:
    from peoplelens.dev.tokens import mint

    print(mint(role=args.role))
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    from peoplelens.dev.client import render

    client = _local_client(args.role)
    session = client.session()
    reply = client.ask(session, args.question)
    print(json.dumps(reply.body, indent=2) if args.json else render(reply.body))
    return 0 if reply.status == 200 else 1


def _cmd_chat(args: argparse.Namespace) -> int:
    from peoplelens.dev.client import render

    client = _local_client(args.role)
    session = client.session()
    print(f"PeopleLens chat as {args.role} (local stack). Type a question, an option id such as o1, or 'quit'.")
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            return 0
        if text in ("quit", "exit"):
            return 0
        if not text:
            continue
        reply = client.choose(session, text) if text.startswith("o") and text[1:].isdigit() else client.ask(session, text)
        print(render(reply.body))


def _cmd_audit_tail(args: argparse.Namespace) -> int:
    from peoplelens.warehouse.audit_sink import AuditSink

    for row in AuditSink().tail(args.limit):
        print(json.dumps({k: v for k, v in row.items() if v is not None}))
    return 0


def _cmd_audit_commit(args: argparse.Namespace) -> int:
    """Local committer: .dev/audit-staging into the pyiceberg SqlCatalog table (same code as production)."""
    import time

    from peoplelens.paths import AUDIT_STAGING_DIR
    from peoplelens.warehouse.audit_commit import commit, local_store, staged_ids
    from peoplelens.warehouse.audit_sink import local_catalog

    import shutil
    from pathlib import Path

    store = local_store(AUDIT_STAGING_DIR)
    if args.restage_from:
        # Redeliver objects that were already committed (a replay): the committer must append nothing.
        src = Path(args.restage_from)
        if not src.is_dir():
            print(f"--restage-from {src}: no such directory", file=sys.stderr)
            return 1
        shutil.copytree(src, AUDIT_STAGING_DIR, dirs_exist_ok=True)
    if args.expect_rows and args.wait_s > 0:
        # The local queue delivers batches every few seconds: wait until this run's events are staged.
        deadline = time.monotonic() + args.wait_s
        while len(staged_ids(store)) < args.expect_rows and time.monotonic() < deadline:
            time.sleep(1.0)
    if args.keep_copy:
        # A copy of exactly the objects this run commits (and then deletes), for a later --restage-from.
        dest = Path(args.keep_copy)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(AUDIT_STAGING_DIR, dest)
    result = commit(store, local_catalog())
    print(json.dumps(result.as_dict(), sort_keys=True))
    ok = True
    if args.expect_rows is not None and result.appended != args.expect_rows:
        print(f"expected {args.expect_rows} appended rows, got {result.appended}", file=sys.stderr)
        ok = False
    if args.expect_already is not None and result.already_in_table != args.expect_already:
        print(f"expected {args.expect_already} events already in the table, got {result.already_in_table}", file=sys.stderr)
        ok = False
    if (args.expect_rows is not None or args.expect_already is not None) and result.quarantined:
        print(f"{len(result.quarantined)} staged objects failed the whitelist and were quarantined", file=sys.stderr)
        ok = False
    return 0 if ok else 1


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

    wh = sub.add_parser("warehouse", help="local DuckDB warehouse sidecar (stand-in for BigQuery)")
    wh_sub = wh.add_subparsers(dest="warehouse_command", required=True)
    serve = wh_sub.add_parser("serve", help="serve catalog SQL over HTTP on 127.0.0.1 (token in PL_WAREHOUSE_TOKEN)")
    serve.add_argument("--port", type=int, default=0, help="0 picks an ephemeral port (default)")
    serve.set_defaults(func=_cmd_warehouse_serve)

    dev = sub.add_parser("dev", help="local stack: sidecar, wrangler dev, llama-server")
    dev_sub = dev.add_subparsers(dest="dev_command", required=True)
    up = dev_sub.add_parser("up", help="start the DuckDB sidecar and wrangler dev --local on 8794")
    up.add_argument("--llm", choices=["stub", "openai_compat"], default="stub")
    dev_sub.add_parser("down", help="stop the local stack and verify ports 8794 and 9244 are free")
    dev_sub.add_parser("keys", help="create the local RS256 key pair")
    dev_sub.add_parser("llm-up", help="start llama-server (Qwen3-1.7B) on 8180")
    dev_sub.add_parser("llm-down", help="stop llama-server and verify port 8180 is free")
    dev.set_defaults(func=_cmd_dev)

    roles = ["people_analytics", "hr_business_partner", "talent_acquisition", "executive"]
    token = sub.add_parser("token", help="mint a local Access-shaped JWT for a role")
    token.add_argument("--role", choices=roles, default="people_analytics")
    token.set_defaults(func=_cmd_token)

    ask = sub.add_parser("ask", help="ask the local Worker one question")
    ask.add_argument("question")
    ask.add_argument("--role", choices=roles, default="people_analytics")
    ask.add_argument("--json", action="store_true")
    ask.set_defaults(func=_cmd_ask)

    chat = sub.add_parser("chat", help="multi-turn chat with the local Worker")
    chat.add_argument("--role", choices=roles, default="people_analytics")
    chat.set_defaults(func=_cmd_chat)

    audit = sub.add_parser("audit", help="local audit trail (pyiceberg SqlCatalog)")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    tail = audit_sub.add_parser("tail", help="print the latest local audit events")
    tail.add_argument("--limit", type=int, default=20)
    tail.set_defaults(func=_cmd_audit_tail)
    com = audit_sub.add_parser("commit", help="append the staged audit events to the local Iceberg table (dedupes on request_id)")
    com.add_argument("--expect-rows", type=int, default=None, help="fail unless exactly this many rows are appended")
    com.add_argument("--wait-s", type=float, default=60.0, help="with --expect-rows N: wait up to this long for N staged events")
    com.add_argument("--expect-already", type=int, default=None,
                     help="fail unless exactly this many staged events are found already in the table (a replay)")
    com.add_argument("--keep-copy", default=None, metavar="DIR", help="copy the staged objects to DIR before committing them")
    com.add_argument("--restage-from", default=None, metavar="DIR",
                     help="copy the objects in DIR back into staging first (replays a --keep-copy)")
    com.set_defaults(func=_cmd_audit_commit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
