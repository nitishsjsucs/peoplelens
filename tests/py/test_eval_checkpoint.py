"""Resumable, budget-capped production eval (SPEC.md 12.6, 25.4 C and section 13), run
against a fake Worker and a fake Workers AI REST baseline that can answer
AI_DAILY_LIMIT or 3036 in the middle of an item."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import pytest

from peoplelens.dev.client import Reply
from peoplelens.eval import checkpoint as cp
from peoplelens.eval import report as report_mod
from peoplelens.eval.generate import QUESTIONS_PATH, load_items
from peoplelens.eval.gold import GOLD_PATH, load_gold
from peoplelens.eval.llm_client import DailyAllocationError
from peoplelens.eval.runner import main_prod

HEAD = "a" * 40
IDS = ["A-headcount-01", "A-headcount-02", "CL-01", "CL-11", "AD-DF-01", "AD-PI-01"]
DAY1 = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
DAY2 = datetime(2026, 10, 13, 9, tzinfo=timezone.utc)
SEM_USAGE = {"prompt_tokens": 3600, "completion_tokens": 67, "neurons": 110.0}
BASE_USAGE = {"prompt_tokens": 1430, "completion_tokens": 61}


@pytest.fixture(scope="module")
def items():
    by_id = {i["id"]: i for i in load_items(QUESTIONS_PATH)}
    return [by_id[i] for i in IDS]


@pytest.fixture(scope="module")
def gold():
    return load_gold(GOLD_PATH)


class World:
    """The production Worker as the eval sees it, behind Access."""

    def __init__(self, build: str = HEAD, model: str = cp.PLANNER_MODEL, limit_on: set[str] | None = None):
        self.build, self.model = build, model
        self.limit_on = limit_on or set()
        self.asks: list[str] = []
        self.used = 0.0
        self.sessions = 0

    def client(self, base_url, headers):
        return FakeWorker(self)


class FakeWorker:
    def __init__(self, world: World):
        self.w = world

    def health(self):
        return Reply(200, {"ok": True, "environment": "production", "warehouse": "bigquery", "llm_provider": "workers_ai",
                           "planner_model": self.w.model, "build_commit": self.w.build}, 1.0)

    def me(self):
        return Reply(200, {"role": "executive", "ai_budget": {"day": "d", "used": self.w.used, "cap": 6000}}, 1.0)

    def session(self):
        self.w.sessions += 1
        return f"s{self.w.sessions}"

    def ask(self, session, text):
        self.w.asks.append(text)
        if text in self.w.limit_on:
            return Reply(503, {"request_id": "r", "type": "refusal", "refusal": {"code": "AI_DAILY_LIMIT", "message": "m", "alternatives": []},
                               "meta": {"planner_attempts": 1, "llm_usage": None, "llm_ms": 0, "warehouse_ms": 0}}, 3.0)
        self.w.used += SEM_USAGE["neurons"]
        return Reply(200, {"request_id": "r", "type": "refusal", "refusal": {"code": "OUT_OF_DOMAIN", "message": "m", "alternatives": []},
                           "meta": {"planner_attempts": 1, "llm_usage": dict(SEM_USAGE), "llm_ms": 900, "warehouse_ms": 0}}, 1200.0)


class FakeSql:
    provider, model = "workers_ai", cp.PLANNER_MODEL

    def __init__(self, fail_on: set[str] | None = None):
        self.fail_on = fail_on or set()
        self.calls: list[str] = []

    def complete(self, messages, schema):
        question = messages[-1]["content"]
        self.calls.append(question)
        if any(q in question for q in self.fail_on):
            raise DailyAllocationError("HTTP 429: Workers AI daily free allocation used up (3036)")
        return json.dumps({"sql": "SELECT department, COUNT(*) AS n FROM sem_workforce_monthly GROUP BY department"}), 800.0, dict(BASE_USAGE)


class FakeExecutor:
    kind, dialect = "bigquery", "bigquery"

    def run(self, sql):
        return ["department", "n"], [["Sales", 380]]

    def close(self):
        pass


def args(**kw) -> argparse.Namespace:
    base = {"official": True, "limit": 0, "neurons_today": None, "resume": False, "no_baseline": False, "out": "",
            "dev": False, "llm": "workers_ai", "target": "prod", "assert_safety": False}
    return argparse.Namespace(**{**base, **kw})


def run(tmp_path, items, gold, world, *, day=DAY1, head=HEAD, dirty=False, sql=None, **kw) -> int:
    sql = sql or FakeSql()
    return main_prod(
        args(**kw),
        client_factory=world.client,
        baseline_factory=lambda: (sql, FakeExecutor()),
        identities_fn=lambda target: ("https://peoplelens.example.workers.dev",
                                      {r: {"CF-Access-Client-Id": r} for r in ("people_analytics", "hr_business_partner", "talent_acquisition", "executive")}),
        git=lambda: (head, dirty),
        now=lambda: day,
        root=tmp_path / "eval-prod",
        items=items,
        gold=gold,
        results_dir=tmp_path / "results",
    )


def the_run(tmp_path) -> cp.RunDir:
    runs = [p for p in (tmp_path / "eval-prod").iterdir()]
    assert len(runs) == 1
    return cp.RunDir(runs[0])


def test_daily_budget_formula():
    assert cp.daily_budget({}, 0) == cp.Budget(5000, 2000)
    assert cp.daily_budget({"semantic": 4900, "baseline": 1990}, 0) == cp.Budget(100, 10)
    # the Worker's 6,000 share minus what anyone used today minus the 1,000 interactive reserve
    assert cp.daily_budget({}, 4500) == cp.Budget(500, 2000)
    assert cp.daily_budget({}, 9999) == cp.Budget(0, 2000)
    # --neurons-today lowers the day's total, split 5:2, and never raises it
    assert cp.daily_budget({}, 0, neurons_today=3500) == cp.Budget(2500, 1000)
    assert cp.daily_budget({}, 0, neurons_today=999_999) == cp.Budget(5000, 2000)
    assert cp.daily_budget({}, None, neurons_today=0) == cp.Budget(0, 0)


def test_item_worst_cases(items):
    by_id = {i["id"]: i for i in items}
    assert cp.item_worst_case(by_id["A-headcount-01"], True) == cp.Budget(200, 127)
    assert cp.item_worst_case(by_id["CL-11"], True) == cp.Budget(400, 0)  # clarification items run no baseline
    assert cp.item_worst_case(by_id["AD-DF-01"], True) == cp.Budget(400, 254)
    assert cp.item_worst_case(by_id["AD-DF-01"], False) == cp.Budget(400, 0)
    assert cp.neurons(BASE_USAGE) == 50.63


def test_resume_skips_finished_items_and_assembles_one_report(tmp_path, items, gold):
    world = World()
    # Day 1 at 700 neurons (500 semantic, 200 baseline): three items fit, the two-turn fourth does not.
    assert run(tmp_path, items, gold, world, neurons_today=700) == 0
    r = the_run(tmp_path)
    assert [x["id"] for x in r.records()] == IDS[:3]
    assert r.ledger()["2026-10-12"] == {"semantic": 330.0, "baseline": 101.26, "items": 3}
    assert not list((tmp_path / "results").glob("*")) if (tmp_path / "results").exists() else True
    asked_day1 = list(world.asks)
    # Day 2 continues on the same commit and build; finished items are not asked again.
    assert run(tmp_path, items, gold, world, day=DAY2, resume=True) == 0
    asked_day2 = world.asks[len(asked_day1):]
    assert not set(asked_day2) & set(asked_day1)
    assert [x["id"] for x in r.records()] == IDS
    report = json.loads((tmp_path / "results" / "2026-10-12-prod-llama-3.3-70b.json").read_text())
    run_info = report["run"]
    assert run_info["days"] == 2 and run_info["utc_span"] == ["2026-10-12", "2026-10-13"]
    assert set(run_info["neurons_by_day"]) == {"2026-10-12", "2026-10-13"}
    assert run_info["model"] == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    assert run_info["git_commit"] == HEAD and run_info["build_commit"] == HEAD
    assert report["summary"]["items"] == len(IDS)
    assert r.plan()["report"].endswith("2026-10-12-prod-llama-3.3-70b")
    md = (tmp_path / "results" / "2026-10-12-prod-llama-3.3-70b.md").read_text()
    assert "UTC days: 2 (2026-10-12 to 2026-10-13)" in md and "cold isolates" in md


def test_the_assembled_report_equals_a_single_session_report(tmp_path, items, gold):
    world = World()
    assert run(tmp_path, items, gold, world, neurons_today=700) == 0
    assert run(tmp_path, items, gold, world, day=DAY2, resume=True) == 0
    r = the_run(tmp_path)
    report = json.loads(next((tmp_path / "results").glob("*.json")).read_text())
    by_id = {x["id"]: x for x in r.records()}
    single = report_mod.build({"llm_provider": "workers_ai"}, items, [by_id[i]["semantic"] for i in IDS],
                              [by_id[i]["baseline"] for i in IDS if by_id[i]["baseline"]])
    assert json.loads(json.dumps(single["summary"])) == report["summary"]


def test_a_partial_item_is_discarded_on_ai_daily_limit_and_rerun_whole_next_day(tmp_path, items, gold):
    two_turn = next(i for i in items if i["id"] == "CL-11")
    world = World(limit_on={two_turn["turns"][1]["text"]})
    assert run(tmp_path, items, gold, world) == 0
    r = the_run(tmp_path)
    assert [x["id"] for x in r.records()] == IDS[:3]  # CL-11 discarded: its first turn ran, its second hit the limit
    assert world.asks.count(two_turn["turns"][0]["text"]) == 1
    assert "AI_DAILY_LIMIT" in r.plan()["sessions"][-1]["stopped"]
    world.limit_on = set()
    sessions_before = world.sessions
    assert run(tmp_path, items, gold, world, day=DAY2, resume=True) == 0
    assert [x["id"] for x in r.records()] == IDS
    assert world.asks.count(two_turn["turns"][0]["text"]) == 2  # asked again from its first turn
    cl11 = next(x for x in r.records() if x["id"] == "CL-11")
    assert cl11["day"] == "2026-10-13" and len(cl11["semantic"]["turns"]) == 2
    assert world.sessions > sessions_before


def test_a_rest_3036_mid_item_discards_the_item_and_stops(tmp_path, items, gold):
    probe = next(i for i in items if i["id"] == "AD-DF-01")
    sql = FakeSql(fail_on={probe["turns"][1]["text"]})
    assert run(tmp_path, items, gold, World(), sql=sql) == 0
    r = the_run(tmp_path)
    assert [x["id"] for x in r.records()] == IDS[:4]
    assert "3036" in r.plan()["sessions"][-1]["stopped"]
    assert not list((tmp_path / "results").glob("*.json")) if (tmp_path / "results").exists() else True


def test_a_resume_on_another_commit_build_or_model_is_refused(tmp_path, items, gold, capsys):
    # 450 neurons split 5:2 is 321 semantic and 129 baseline: exactly one item fits.
    assert run(tmp_path, items, gold, World(), neurons_today=450) == 0
    assert len(the_run(tmp_path).records()) == 1
    assert run(tmp_path, items, gold, World(), day=DAY2, resume=True, head="b" * 40) == 2
    assert "start a new run" in capsys.readouterr().err
    assert run(tmp_path, items, gold, World(build="c" * 40), day=DAY2, resume=True) == 2
    assert "deployed build" in capsys.readouterr().err
    assert run(tmp_path, items, gold, World(model="@cf/meta/llama-3.1-8b-instruct-fp8"), day=DAY2, resume=True) == 2
    assert "planner model" in capsys.readouterr().err
    assert run(tmp_path, items, gold, World(), day=DAY2, resume=True, dirty=True) == 2
    assert len(the_run(tmp_path).records()) == 1  # nothing ran on a refused day


def test_a_new_run_needs_the_deployed_build_to_be_head(tmp_path, items, gold, capsys):
    assert run(tmp_path, items, gold, World(build="d" * 40)) == 2
    assert "make deploy" in capsys.readouterr().err
    assert run(tmp_path, items, gold, World(build=HEAD + "-dirty")) == 2
    assert run(tmp_path, items, gold, World(), dirty=True) == 2
    assert not (tmp_path / "eval-prod").exists()


def test_resume_without_a_run_explains_how_to_start(tmp_path, items, gold, capsys):
    assert run(tmp_path, items, gold, World(), resume=True) == 2
    assert "--official" in capsys.readouterr().err


def test_rest_3036_is_recognized_by_code_or_message():
    from peoplelens.eval.llm_client import is_daily_allocation

    body = json.dumps({"success": False, "errors": [{"code": 3036, "message": "You have used up your daily free allocation of 10,000 neurons."}]})
    assert is_daily_allocation(429, body.encode())
    assert is_daily_allocation(429, b'{"errors": [{"message": "You have used up your daily free allocation of 10,000 neurons."}]}')
    assert not is_daily_allocation(429, b'{"errors": [{"code": 3040, "message": "Capacity temporarily exceeded"}]}')
    assert not is_daily_allocation(500, b"oops")

