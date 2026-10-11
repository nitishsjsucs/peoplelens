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
