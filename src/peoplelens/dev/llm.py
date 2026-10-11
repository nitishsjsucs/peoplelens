"""llama-server for the local LLM (`make llm-up` / `make llm-down`).

A local stand-in for Workers AI: Qwen3-1.7B (Q4_0) through llama-server's
OpenAI-compatible API on 127.0.0.1:8180, one slot, 8k context, all layers on GPU.
"""

from __future__ import annotations

import os
import shutil
import urllib.request
from pathlib import Path

from peoplelens.dev import procs

DEFAULT_MODEL = Path.home() / "Developer" / "projects" / "_models" / "Qwen3-1.7B-Q4_0-rtn.gguf"
MODEL_NAME = "qwen3-1.7b"
BASE_URL = f"http://127.0.0.1:{procs.LLM_PORT}/v1"


def model_path() -> Path:
    return Path(os.environ.get("PL_LLM_MODEL", str(DEFAULT_MODEL))).expanduser()


def healthy() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{procs.LLM_PORT}/health", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def up(timeout: float = 120.0) -> int:
    binary = shutil.which("llama-server")
    if not binary:
        print("llama-server not found on PATH (brew install llama.cpp)")
        return 2
    model = model_path()
    if not model.exists():
        print(f"model file missing: {model} (set PL_LLM_MODEL)")
        return 2
    if healthy() and "llama" in procs.read_pids():
        print(f"llama-server already running on {procs.LLM_PORT}")
        return 0
    if not procs.port_free(procs.LLM_PORT):
        print(f"port {procs.LLM_PORT} is in use by another process")
        return 2
    cmd = [binary, "-m", str(model), "--host", "127.0.0.1", "--port", str(procs.LLM_PORT),
           "-np", "1", "-c", "8192", "-ngl", "99", "--alias", MODEL_NAME]
    proc = procs.start("llama", cmd)
    procs.record("llama", proc, {"port": procs.LLM_PORT})
    if not procs.wait_for(healthy, timeout):
        procs.stop("llama")
        print("llama-server did not become healthy; see .dev/logs/llama.log")
        return 1
    print(f"llama-server ready at {BASE_URL} ({model.name})")
    return 0


def down() -> int:
    procs.stop("llama")
    if not procs.wait_for(lambda: procs.port_free(procs.LLM_PORT), 10):
        print(f"port {procs.LLM_PORT} is still in use")
        return 1
    print("llama-server stopped; port 8180 free")
    return 0
