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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
