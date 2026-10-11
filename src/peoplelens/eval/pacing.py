"""Client-side pacing: each identity stays at or under 25 requests per minute
(the server allows 30), so evals exercise the same limits as users and never
see RATE_LIMITED. Waiting time is never counted as latency."""

from __future__ import annotations

import time
from collections import deque


class Pacer:
    def __init__(self, per_minute: int = 25, window_s: float = 60.0, clock=time.monotonic, sleep=time.sleep):
        self.per_minute = per_minute
        self.window_s = window_s
        self.clock = clock
        self.sleep = sleep
        self.sent: dict[str, deque[float]] = {}
        self.waited_s = 0.0

    def wait(self, identity: str) -> float:
        q = self.sent.setdefault(identity, deque())
        now = self.clock()
        while q and now - q[0] >= self.window_s:
            q.popleft()
        waited = 0.0
        if len(q) >= self.per_minute:
            waited = self.window_s - (now - q[0]) + 0.05
            self.sleep(waited)
            now = self.clock()
            while q and now - q[0] >= self.window_s:
                q.popleft()
        q.append(self.clock())
        self.waited_s += waited
        return waited


def interleave(items: list[dict], key: str = "role") -> list[dict]:
    """Round-robin across identities, keeping each identity's order."""
    queues: dict[str, list[dict]] = {}
    for item in items:
        queues.setdefault(item[key], []).append(item)
    out: list[dict] = []
    while any(queues.values()):
        for k in sorted(queues):
            if queues[k]:
                out.append(queues[k].pop(0))
    return out
