"""Resumable, budget-capped production eval (SPEC.md 12.6 and 25.4 C).

On the Workers Free plan one production eval needs about 31,400 Workers AI neurons
(about 60,700 at the upper bound) while the whole account gets 10,000 per UTC day,
shared with three other repos. So `make eval-prod` checkpoints per item and spends
at most a daily share, over about 5 UTC days (12 at most):

    .dev/eval-prod/<run_id>/plan.json       commit, deployed build, model, item order
    .dev/eval-prod/<run_id>/progress.jsonl  one line per finished item (semantic and
                                            baseline results, neurons, UTC day)
    .dev/eval-prod/<run_id>/ledger.json     neurons per UTC day and path

Day 1: `make eval-prod ARGS="--official"`; every later day:
`make eval-prod ARGS="--official --resume"`. Every day must run on the plan's commit
and deployed build. An item is atomic: if Workers AI's allowance runs out in the
middle of it, its partial results are discarded and the run stops for the day.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from peoplelens.paths import DEV_DIR
from peoplelens.policy import load

CHECKPOINT_ROOT = DEV_DIR / "eval-prod"
FREE = load().limits["free_plan"]
PLANNER_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
BASELINE_SLICES = ("answerable", "adversarial")


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()


def neurons(usage: dict | None, model: str = PLANNER_MODEL) -> float | None:
    """Neurons for a token usage at the model's pinned rates; None when unknown."""
    rates = FREE["model_neurons_per_m_tokens"].get(model)
    if not usage or not rates or ("prompt_tokens" not in usage and "completion_tokens" not in usage):
        return None
    n = (usage.get("prompt_tokens") or 0) * rates["input"] + (usage.get("completion_tokens") or 0) * rates["output"]
    return round(n / 1_000_000, 2)


@dataclass
class Budget:
    semantic: float
    baseline: float


def daily_budget(spent_today: dict, ai_budget_used: float | None, neurons_today: float | None = None, free: dict = FREE) -> Budget:
    """This UTC day's budgets (SPEC.md 12.6 and 25.4 C3).

    semantic = min(eval_semantic_neurons_per_day - eval's semantic spend today,
                   worker_neurons_per_day - ai_budget.used - interactive_reserve_neurons)
    baseline = eval_baseline_neurons_per_day - eval's baseline spend today
    `--neurons-today n` may lower the day's total, split 5:2 between the paths, never raise it.
    """
    sem_cap = float(free["eval_semantic_neurons_per_day"])
    base_cap = float(free["eval_baseline_neurons_per_day"])
    if neurons_today is not None:
        total = sem_cap + base_cap
        share = max(0.0, min(float(neurons_today), total))
        sem_cap, base_cap = share * sem_cap / total, share * base_cap / total
    semantic = sem_cap - float(spent_today.get("semantic", 0.0))
    if ai_budget_used is not None:
        semantic = min(semantic, free["worker_neurons_per_day"] - float(ai_budget_used) - free["interactive_reserve_neurons"])
    baseline = base_cap - float(spent_today.get("baseline", 0.0))
    return Budget(max(0.0, round(semantic, 2)), max(0.0, round(baseline, 2)))


def item_worst_case(item: dict, with_baseline: bool, free: dict = FREE) -> Budget:
    """An item starts only if its worst case fits: 200 per turn through the planner and
    130 per baseline turn (only answerable and adversarial items run the baseline)."""
    turns = len(item["turns"])
    base_turns = turns if with_baseline and item["slice"] in BASELINE_SLICES else 0
    return Budget(float(turns * free["planner_reserve_neurons"]), float(base_turns * free["baseline_reserve_neurons"]))


def fits(item: dict, budget: Budget, with_baseline: bool) -> bool:
    need = item_worst_case(item, with_baseline)
    return need.semantic <= budget.semantic and need.baseline <= budget.baseline


def semantic_neurons(record: dict, free: dict = FREE) -> float:
    """Neurons the Worker reported for an item's turns; the reservation when unknown."""
    total = 0.0
    for turn in record["turns"]:
        meta = turn.get("meta") or {}
        usage = meta.get("llm_usage") or {}
        if isinstance(usage.get("neurons"), (int, float)):
            total += float(usage["neurons"])
        elif meta.get("planner_attempts"):
            total += float(meta["planner_attempts"]) * free["planner_reserve_neurons"]
    return round(total, 2)


def baseline_neurons(record: dict | None, model: str = PLANNER_MODEL, free: dict = FREE) -> float:
    if not record:
        return 0.0
    total = 0.0
    for turn in record["turns"]:
        n = neurons(turn.get("usage"), model)
        total += n if n is not None else float(free["baseline_reserve_neurons"])
    return round(total, 2)


class RunDir:
    def __init__(self, path: Path):
        self.path = path

    @property
    def plan_path(self) -> Path:
        return self.path / "plan.json"

    @property
    def progress_path(self) -> Path:
        return self.path / "progress.jsonl"

    @property
    def ledger_path(self) -> Path:
        return self.path / "ledger.json"

    @classmethod
    def create(cls, root: Path, plan: dict) -> "RunDir":
        run_id = f"{plan['started_day']}-{plan['commit'][:7]}"
        path = root / run_id
        n = 2
        while path.exists():
            path = root / f"{run_id}-{n}"
            n += 1
        path.mkdir(parents=True)
        run = cls(path)
        run.plan_path.write_text(json.dumps({**plan, "run_id": path.name}, indent=1, sort_keys=True) + "\n")
        run.ledger_path.write_text("{}\n")
        run.progress_path.touch()
        return run

    @classmethod
    def latest_unfinished(cls, root: Path) -> "RunDir | None":
        if not root.exists():
            return None
        runs = [cls(p) for p in root.iterdir() if (p / "plan.json").exists()]
        runs = [r for r in runs if not r.plan().get("report")]
        return max(runs, key=lambda r: r.plan()["started_at"]) if runs else None

    def plan(self) -> dict:
        return json.loads(self.plan_path.read_text())

    def update_plan(self, **fields) -> None:
        plan = self.plan()
        plan.update(fields)
        self.plan_path.write_text(json.dumps(plan, indent=1, sort_keys=True) + "\n")

    def records(self) -> list[dict]:
        lines = self.progress_path.read_text().splitlines() if self.progress_path.exists() else []
        return [json.loads(line) for line in lines if line.strip()]

    def done_ids(self) -> set[str]:
        return {r["id"] for r in self.records()}

    def ledger(self) -> dict[str, dict[str, float]]:
        return json.loads(self.ledger_path.read_text())

    def record_item(self, record: dict) -> None:
        """Append one finished item and add its neurons to the day's ledger (in that order:
        a crash in between leaves the ledger short, never an item counted twice)."""
        with self.progress_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        ledger = self.ledger()
        day = ledger.setdefault(record["day"], {"semantic": 0.0, "baseline": 0.0, "items": 0})
        day["semantic"] = round(day["semantic"] + record["neurons"]["semantic"], 2)
        day["baseline"] = round(day["baseline"] + record["neurons"]["baseline"], 2)
        day["items"] += 1
        self.ledger_path.write_text(json.dumps(ledger, indent=1, sort_keys=True) + "\n")


def resume_problems(plan: dict, head: str, dirty: bool, health: dict, official: bool) -> list[str]:
    """Why this day may not continue the run: every day runs on one commit and one deployed build."""
    problems = []
    if official and dirty:
        problems.append("the working tree is dirty (an official run needs a clean tree)")
    if head != plan["commit"]:
        problems.append(f"HEAD is {head[:12]}, but the run started on {plan['commit'][:12]}: start a new run")
    if health.get("build_commit") != plan["build_commit"]:
        problems.append(f"the deployed build is {health.get('build_commit')}, but the run started on build {plan['build_commit']}: start a new run")
    if health.get("planner_model") != plan["model"]:
        problems.append(f"the planner model is {health.get('planner_model')}, but the run started with {plan['model']}: start a new run")
    return problems


def assemble(run: RunDir, items: list[dict]) -> tuple[list[dict], list[dict] | None, dict]:
    """Semantic and baseline records in the plan's item order, plus the multi-day fields."""
    by_id = {r["id"]: r for r in run.records()}
    order = [i["id"] for i in items if i["id"] in by_id]
    semantic = [by_id[i]["semantic"] for i in order]
    baseline_records = [by_id[i]["baseline"] for i in order if by_id[i].get("baseline")]
    plan = run.plan()
    ledger = run.ledger()
    days = sorted(ledger)
    extra = {
        "days": len(days),
        "utc_span": [days[0], days[-1]] if days else None,
        "neurons_by_day": ledger,
        "model": plan["model"],
        "run_id": plan["run_id"],
        "build_commit": plan["build_commit"],
        "latency_note": "latency percentiles pool items from every day; each UTC day starts on cold isolates",
    }
    return semantic, (baseline_records if plan.get("baseline") else None), extra
