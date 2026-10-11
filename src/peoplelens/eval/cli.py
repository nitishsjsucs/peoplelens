"""`pl-eval`: generate the eval set, build gold, run evaluations."""

from __future__ import annotations

import argparse
import sys


def _generate(args: argparse.Namespace) -> int:
    from peoplelens.eval.generate import write

    return write(check=args.check)


def _gold(args: argparse.Namespace) -> int:
    from peoplelens.eval.generate import QUESTIONS_PATH, load_items
    from peoplelens.eval.gold import GOLD_PATH, build, dump

    items = load_items(QUESTIONS_PATH)
    text = dump(build(items))
    if args.check:
        if not GOLD_PATH.exists() or GOLD_PATH.read_text(encoding="utf-8") != text:
            print("DRIFT: eval/gold.json differs from the reference implementation; run `uv run pl-eval gold`", file=sys.stderr)
            return 1
        print("OK eval/gold.json matches the reference implementation")
        return 0
    GOLD_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {GOLD_PATH}")
    return 0


def _run(args: argparse.Namespace) -> int:
    from peoplelens.eval.runner import main as run_main

    return run_main(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pl-eval", description="PeopleLens evaluation harness")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="write eval/questions.jsonl and eval/dev_questions.jsonl")
    gen.add_argument("--check", action="store_true")
    gen.set_defaults(func=_generate)
    gold = sub.add_parser("gold", help="write eval/gold.json from the reference implementation")
    gold.add_argument("--check", action="store_true")
    gold.set_defaults(func=_gold)
    run = sub.add_parser("run", help="run the eval against the local stack or production")
    run.add_argument("--llm", choices=["stub", "openai_compat", "workers_ai"], default="stub")
    run.add_argument("--target", choices=["local", "prod"], default="local")
    run.add_argument("--assert-safety", action="store_true", help="CI gate for the stub run")
    run.add_argument("--official", action="store_true", help="refuse a dirty tree; README numbers come only from official runs")
    run.add_argument("--no-baseline", action="store_true", help="skip the text-to-SQL baseline")
    run.add_argument("--limit", type=int, default=0, help="run only the first N items (debugging; never official)")
    run.add_argument("--out", default="", help="report path stem (default eval/results/<date>-<target>-<model>)")
    run.add_argument("--dev", action="store_true", help="run the dev set instead (prompt iteration only; needs --out)")
    run.add_argument("--resume", action="store_true", help="prod: continue the unfinished run (same commit and deployed build)")
    run.add_argument("--neurons-today", type=float, default=None,
                     help="prod: lower today's Workers AI budget (split 5:2 between the semantic path and the baseline); never raises it")
    run.set_defaults(func=_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
