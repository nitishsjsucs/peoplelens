"""Process-group liveness for `pl dev down`: zombies do not count as running."""

from __future__ import annotations

import os
import signal
import subprocess
import time

from peoplelens.dev import procs


def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_running_group_is_alive_and_a_zombie_group_is_not():
    live = subprocess.Popen(["sleep", "30"], start_new_session=True)
    done = subprocess.Popen(["sleep", "0"], start_new_session=True)
    try:
        assert procs.group_alive(os.getpgid(live.pid))
        # `done` exits but is not reaped yet (no wait()), so it is a zombie in its own group.
        assert _wait(lambda: (procs._group_states(done.pid) or ["?"])[0].startswith("Z"))
        assert not procs.group_alive(done.pid)
    finally:
        os.killpg(os.getpgid(live.pid), signal.SIGKILL)
        live.wait()
        done.wait()
    assert not procs.group_alive(done.pid)
