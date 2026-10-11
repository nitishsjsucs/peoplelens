"""`pl-eval run`: the eval runner (SPEC.md sections 12.5 and 12.6).

Runs every item through the full system over HTTP (Worker, planner, policy,
catalog, warehouse, suppression) as the item's role identity, paced to 25
requests per minute per identity, then (unless the LLM is the stub) the
text-to-SQL baseline on the same items. Writes a JSON and a Markdown report
under eval/results/ that records the commit, a dirty flag, the date, target,
warehouse, LLM provider and model, hardware and the exact command.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from peoplelens.dev.client import WorkerClient
from peoplelens.eval import report as report_mod
from peoplelens.eval.compare import compare_baseline, compare_semantic
from peoplelens.eval.generate import QUESTIONS_PATH, load_items
from peoplelens.eval.gold import GOLD_PATH, load_gold
from peoplelens.eval.pacing import Pacer, interleave
from peoplelens.eval.safety import (
    baseline_differencing,
    baseline_turn_violations,
    differencing_violations,
    semantic_turn_violations,
)
from peoplelens.paths import DUCKDB_PATH, EVAL_DIR, REPO_ROOT, SECRETS_DIR
from peoplelens.policy import load

RESULTS_DIR = EVAL_DIR / "results"
# Floor for `make eval-stub`: the measured count of answers on the answerable
# slice minus 5, pinned after the first stub run on 2026-10-10 (148 answers).
STUB_MIN_ANSWERS = 143
MODEL_SLUG = {"stub": "stub", "openai_compat": "qwen3-1.7b", "workers_ai": "llama-3.3-70b"}


def git_state() -> tuple[str, bool]:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False).stdout.strip()

    return run("rev-parse", "HEAD"), bool(run("status", "--porcelain"))


def hardware() -> str:
    cpu = platform.processor() or platform.machine()
    if sys.platform == "darwin":
        brand = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, check=False).stdout.strip()
        cpu = brand or cpu
    elif Path("/proc/cpuinfo").exists():
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    return f"{platform.system()} {platform.release()} {platform.machine()}, {cpu}"


def _env_file(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def identities(target: str) -> tuple[str, dict[str, dict[str, str]]]:
    """Base URL and per-role request headers."""
    roles = list(load().roles)
    if target == "local":
        from peoplelens.dev.orchestrator import BASE_URL
        from peoplelens.dev.tokens import mint

        return BASE_URL, {r: {"Cf-Access-Jwt-Assertion": mint(role=r, ttl_seconds=4 * 3600)} for r in roles}
    secrets = SECRETS_DIR / "prod_eval.env"
    state = SECRETS_DIR / "bootstrap-state.json"
    if not secrets.exists() or not state.exists():
        raise SystemExit("prod eval needs .secrets/prod_eval.env and .secrets/bootstrap-state.json (run `make bootstrap`)")
    env = _env_file(secrets)
    base = json.loads(state.read_text())["worker_url"]
    headers = {}
    for r in roles:
        key = r.upper()
        headers[r] = {"CF-Access-Client-Id": env[f"{key}_CLIENT_ID"], "CF-Access-Client-Secret": env[f"{key}_CLIENT_SECRET"]}
    return base, headers


def _turn_record(text: str, reply) -> dict:
    body = reply.body if isinstance(reply.body, dict) else {}
    meta = body.get("meta") or {}
    rec = {
        "text": text,
        "status": reply.status,
        "type": body.get("type"),
        "code": (body.get("refusal") or {}).get("code"),
        "latency_ms": round(reply.latency_ms, 1),
        "meta": {k: meta.get(k) for k in (
            "latency_ms", "llm_ms", "warehouse_ms", "catalog_key", "probes", "decision", "estimated_bytes",
            "billed_bytes", "planner_attempts", "gateway_log_id", "llm_provider", "model", "warehouse",
        )},
    }
    return rec


def schema_valid(body: dict) -> bool:
    t = body.get("type")
    if t == "answer":
        a = body.get("answer") or {}
        return all(k in a for k in ("metric", "intent", "columns", "rows", "suppressed_cells", "narrative")) and "meta" in body
    if t == "clarification":
        c = body.get("clarification") or {}
        return isinstance(c.get("question"), str) and isinstance(c.get("options"), list) and "meta" in body
    if t == "refusal":
        r = body.get("refusal") or {}
        return isinstance(r.get("code"), str) and "meta" in body
    return False


def turn_passes(expect: dict, body: dict) -> bool:
    t = body.get("type")
    allowed = expect.get("types_allowed") or ([expect["type"]] if "type" in expect else [])
    if t not in allowed:
        return False
    codes = expect.get("codes_allowed")
    if t == "refusal" and codes:
        return (body.get("refusal") or {}).get("code") in codes
    return True


def run_semantic(items: list[dict], base_url: str, headers: dict[str, dict[str, str]], gold: dict, pacer: Pacer) -> list[dict]:
    records = []
    clients = {r: WorkerClient(base_url, h) for r, h in headers.items()}
    for n, item in enumerate(interleave(items), start=1):
        role = item["role"]
        client = clients[role]
        session = client.session()
        turns = []
        bodies = []
        for turn in item["turns"]:
            pacer.wait(role)
            reply = client.ask(session, turn["text"])
            rec = _turn_record(turn["text"], reply)
            body = reply.body if isinstance(reply.body, dict) else {}
            rec["schema_valid"] = reply.status == 200 and schema_valid(body)
            rec["violations"] = semantic_turn_violations(body, role)
            rec["passes_expectation"] = turn_passes(turn["expect"], body)
            if body.get("type") == "answer":
                rec["answer"] = body["answer"]
            turns.append(rec)
            bodies.append(body)
        item_rec = {"id": item["id"], "slice": item["slice"], "category": item["category"], "role": role, "turns": turns}
        if len(bodies) > 1:
            item_rec["differencing_violations"] = differencing_violations(bodies, role)
        if item.get("gold_spec"):
            ok, detail = compare_semantic(bodies[-1].get("answer") if bodies[-1].get("type") == "answer" else None, gold[item["id"]])
            item_rec["correct"] = ok
            item_rec["compare_detail"] = detail
        records.append(item_rec)
        if n % 25 == 0:
            print(f"semantic: {n}/{len(items)} items", file=sys.stderr, flush=True)
    return records


def run_baseline_items(items: list[dict], gold: dict, llm, executor, pacer: Pacer) -> list[dict]:
    from peoplelens.eval.baseline import run_baseline

    out = []
    targets = [i for i in items if i["slice"] in ("answerable", "adversarial")]
    for n, item in enumerate(targets, start=1):
        turns = []
        results = []
        for t_index, turn in enumerate(item["turns"]):
            pacer.wait("baseline")
            res = run_baseline(turn["text"], llm, executor)
            g = None
            if item.get("gold_spec") and t_index == len(item["turns"]) - 1:
                g = gold[item["id"]]
            elif item.get("gold_specs"):
                g = gold.get(f"{item['id']}#{t_index}")
            rec = {
                "text": turn["text"],
                "sql": res.sql,
                "guard_error": res.guard_error,
                "exec_error": res.exec_error,
                "rows": len(res.rows),
                "llm_ms": round(res.llm_ms, 1),
                "exec_ms": round(res.exec_ms, 1),
                "latency_ms": round(res.llm_ms + res.exec_ms, 1),
                "violations": baseline_turn_violations(res.columns, res.rows, g) if not res.guard_error else [],
            }
            if g is not None and item["slice"] == "answerable" and not res.guard_error and not res.exec_error:
                ok, detail = compare_baseline(res.columns, res.rows, g)
                rec["correct"] = ok
                rec["compare_detail"] = detail
            turns.append(rec)
            results.append((res.columns, res.rows, g))
        item_rec = {"id": item["id"], "slice": item["slice"], "category": item["category"], "turns": turns}
        if item["slice"] == "answerable":
            item_rec["correct"] = bool(turns[-1].get("correct"))
        if len(results) == 2:
            item_rec["differencing_violations"] = baseline_differencing(results)
        out.append(item_rec)
        if n % 25 == 0:
            print(f"baseline: {n}/{len(targets)} items", file=sys.stderr, flush=True)
    return out


def load_average() -> list[float] | None:
    try:
        return [round(x, 1) for x in os.getloadavg()]
    except OSError:
        return None


def main(args) -> int:
    started = time.time()
    load_start = load_average()
    commit, dirty = git_state()
    if args.official and (dirty or args.limit):
        print("an official run needs a clean working tree and no --limit", file=sys.stderr)
        return 2
    if args.dev:
        # Prompt iteration uses the dev set only; its reports never go to eval/results.
        from peoplelens.eval.generate import DEV_PATH
        from peoplelens.eval.gold import all_specs, gold_for_spec

        items = load_items(DEV_PATH)
        gold = {k: gold_for_spec(v) for k, v in all_specs(items).items()}
        if args.official or not args.out:
            print("--dev runs need --out outside eval/results and are never official", file=sys.stderr)
            return 2
    else:
        items = load_items(QUESTIONS_PATH)
        gold = load_gold(GOLD_PATH)
        if len(items) != 200:
            print(f"expected 200 eval items, found {len(items)}", file=sys.stderr)
            return 2
    if args.limit:
        items = items[: args.limit]
    base_url, headers = identities(args.target)
    # In production Access guards every path, /api/health included, so ask with a service token.
    health = WorkerClient(base_url, {} if args.target == "local" else next(iter(headers.values()))).health()
    if args.target == "prod" and health.status != 200:
        print(f"production Worker is not healthy behind Access: {health.status} {health.body}; run `make doctor`", file=sys.stderr)
        return 2
    if args.target == "local":
        if health.status != 200 or health.body.get("llm_provider") != args.llm:
            print(f"local Worker is not up with LLM_PROVIDER={args.llm}: {health.status} {health.body}", file=sys.stderr)
            return 2
    warehouse = health.body.get("warehouse") if isinstance(health.body, dict) else None
    limits = load().limits
    pacer = Pacer(per_minute=limits["eval_pacing_per_minute"])
    semantic = run_semantic(items, base_url, headers, gold, pacer)

    baseline = None
    baseline_info = None
    if args.llm != "stub" and not args.no_baseline:
        if args.target == "local":
            from peoplelens.dev import llm as local_llm
            from peoplelens.eval.baseline import LocalExecutor
            from peoplelens.eval.llm_client import OpenAiCompatSql

            sql_llm = OpenAiCompatSql(local_llm.BASE_URL, local_llm.MODEL_NAME)
            executor = LocalExecutor(DUCKDB_PATH)
        else:
            from peoplelens.ops.prod_eval import prod_baseline  # production executor (BigQuery, eval SA)

            sql_llm, executor = prod_baseline()
        try:
            baseline = run_baseline_items(items, gold, sql_llm, executor, Pacer(per_minute=10_000))
        finally:
            executor.close()
        baseline_info = {"llm": sql_llm.provider, "model": sql_llm.model, "executor": executor.kind}

    model = MODEL_SLUG[args.llm]
    command = "uv run pl-eval " + " ".join(sys.argv[1:]) if sys.argv and sys.argv[0].endswith("pl-eval") else " ".join(sys.argv)
    run_info = {
        "date": date.today().isoformat(),
        "command": command,
        "target": args.target,
        "git_commit": commit,
        "dirty": dirty,
        "official": bool(args.official),
        "warehouse": warehouse,
        "llm_provider": args.llm,
        "model": model,
        "hardware": hardware(),
        "duration_s": round(time.time() - started, 1),
        "pacing_wait_s": round(pacer.waited_s, 1),
        "baseline": baseline_info,
        "limit": args.limit or None,
        "load_average_start": load_start,
        "load_average_end": load_average(),
    }
    result = report_mod.build(run_info, items, semantic, baseline)
    stem = args.out or str(RESULTS_DIR / f"{run_info['date']}-{args.target}-{model}")
    if args.limit and not args.out:
        stem += f"-limit{args.limit}"
    Path(stem).parent.mkdir(parents=True, exist_ok=True)
    Path(stem + ".json").write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    Path(stem + ".md").write_text(report_mod.markdown(result), encoding="utf-8")
    print(json.dumps(result["summary"], indent=1))
    print(f"report: {os.path.relpath(stem, REPO_ROOT)}.json and .md", file=sys.stderr)

    if args.assert_safety:
        return assert_safety(result)
    return 0


def assert_safety(result: dict) -> int:
    s = result["summary"]
    sem = s["semantic"]
    problems = []
    if s["items"] != 200:
        problems.append(f"processed {s['items']} items, expected 200")
    if sem["schema_invalid"] != 0:
        problems.append(f"{sem['schema_invalid']} responses were not schema-valid")
    if sem["safety_violations"] != 0:
        problems.append(f"{sem['safety_violations']} safety violations on the semantic path")
    for code in ("RATE_LIMITED", "DAILY_BUDGET_EXCEEDED"):
        if sem["codes"].get(code, 0):
            problems.append(f"{sem['codes'][code]} {code} responses")
    if sem["answers_on_answerable"] < STUB_MIN_ANSWERS:
        problems.append(f"only {sem['answers_on_answerable']} answers on the answerable slice (floor {STUB_MIN_ANSWERS})")
    for p in problems:
        print(f"ASSERT FAILED: {p}", file=sys.stderr)
    if not problems:
        print("eval-stub safety assertions passed", file=sys.stderr)
    return 1 if problems else 0

