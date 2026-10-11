"""Process groups, PID records and port checks for the local stack.

Every child starts in its own process group (start_new_session=True) and is
stopped with os.killpg(SIGTERM), then SIGKILL after 5 s, so wrangler's workerd
children never outlive `pl dev down`.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from peoplelens.paths import DEV_DIR

PIDS_PATH = DEV_DIR / "pids.json"
LOG_DIR = DEV_DIR / "logs"
WRANGLER_PORT = 8794
INSPECTOR_PORT = 9244
LLM_PORT = 8180


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) != 0


def read_pids() -> dict[str, dict]:
    if not PIDS_PATH.exists():
        return {}
    try:
        return json.loads(PIDS_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def write_pids(pids: dict[str, dict]) -> None:
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    PIDS_PATH.write_text(json.dumps(pids, indent=2) + "\n")


def record(name: str, proc: subprocess.Popen, extra: dict | None = None) -> None:
    pids = read_pids()
    pids[name] = {"pid": proc.pid, "pgid": os.getpgid(proc.pid), **(extra or {})}
    write_pids(pids)


def start(name: str, cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
          stdout_pipe: bool = False) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / f"{name}.log", "ab")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE if stdout_pipe else log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop(name: str, grace: float = 5.0) -> bool:
    """Stop a recorded process group. Returns True if something was stopped."""
    pids = read_pids()
    entry = pids.pop(name, None)
    write_pids(pids)
    if not entry:
        return False
    pgid = entry.get("pgid") or entry["pid"]
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.time() + grace
    while time.time() < deadline:
        if not group_alive(pgid):
            return True
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def wait_for(predicate, timeout: float, interval: float = 0.25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def leftover_groups() -> list[int]:
    """Recorded process groups that still have live members."""
    return [e.get("pgid", e["pid"]) for e in read_pids().values() if group_alive(e.get("pgid", e["pid"]))]
