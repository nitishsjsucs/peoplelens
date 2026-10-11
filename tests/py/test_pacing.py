"""Client pacing: at most 25 requests per identity in any 60 s window."""

from __future__ import annotations

from peoplelens.eval.pacing import Pacer, interleave
from peoplelens.eval.report import dist, percentile


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_25_per_minute_per_identity():
    clock = FakeClock()
    p = Pacer(per_minute=25, clock=clock.now, sleep=clock.sleep)
    waits = []
    for _ in range(26):
        waits.append(p.wait("analyst"))
        clock.t += 0.1
    assert sum(waits[:25]) == 0
    assert waits[25] > 0
    assert p.wait("exec") == 0  # identities are independent


def test_never_exceeds_window_rate():
    clock = FakeClock()
    p = Pacer(per_minute=25, clock=clock.now, sleep=clock.sleep)
    times = []
    for _ in range(100):
        p.wait("a")
        times.append(clock.t)
        clock.t += 0.5
    for i in range(len(times)):
        assert sum(1 for t in times if times[i] <= t < times[i] + 60) <= 25


def test_interleave_round_robin():
    items = [{"role": r, "n": i} for i, r in enumerate(["a", "a", "a", "b", "c", "c"])]
    assert [i["role"] for i in interleave(items)] == ["a", "b", "c", "a", "c", "a"]


def test_percentiles():
    vals = list(range(1, 101))
    assert percentile(vals, 95) == 95
    assert percentile(vals, 50) == 50
    assert percentile([], 95) is None
    assert dist([1.0, 2.0])["max"] == 2.0


def test_stub_reports_never_include_accuracy():
    from peoplelens.eval.report import build, markdown

    items = [{"id": "A1", "slice": "answerable", "category": "headcount", "turns": [{"text": "q"}]}]
    turn = {"type": "answer", "code": None, "latency_ms": 5.0, "schema_valid": True, "violations": [],
            "passes_expectation": True, "meta": {"llm_ms": 0, "warehouse_ms": 1}}
    semantic = [{"id": "A1", "slice": "answerable", "category": "headcount", "turns": [turn], "correct": True}]
    run = {"llm_provider": "stub", "target": "local", "model": "stub", "date": "2026-10-10", "command": "x",
           "git_commit": "c", "dirty": False, "official": False, "warehouse": "duckdb_local", "hardware": "h",
           "duration_s": 1, "pacing_wait_s": 0}
    result = build(run, items, semantic, None)
    sem = result["summary"]["semantic"]
    assert sem["result_accuracy"] is None and sem["meets_accuracy_target"] is None
    assert "correct" not in result["semantic_items"][0]
    md = markdown(result)
    assert "not reported" in md and "100.0%" not in md
    run["llm_provider"] = "openai_compat"
    semantic[0]["correct"] = True
    assert build(run, items, semantic, None)["summary"]["semantic"]["result_accuracy"] == 1.0


def test_assert_safety_fails_on_any_server_limit_code(capsys):
    from peoplelens.eval.runner import SERVER_LIMIT_CODES, STUB_MIN_ANSWERS, assert_safety

    def result(codes: dict) -> dict:
        return {"summary": {"items": 200, "semantic": {"schema_invalid": 0, "safety_violations": 0, "codes": codes,
                                                       "answers_on_answerable": STUB_MIN_ANSWERS}}}

    assert SERVER_LIMIT_CODES == ("RATE_LIMITED", "DAILY_BUDGET_EXCEEDED", "AI_DAILY_LIMIT")
    assert assert_safety(result({"PII_REQUEST": 3})) == 0
    for code in SERVER_LIMIT_CODES:
        assert assert_safety(result({code: 1})) == 1
        assert code in capsys.readouterr().err
