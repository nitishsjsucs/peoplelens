"""`pl dev up` / `pl dev down`: the local stack.

up: keys, .dev.vars, fresh Miniflare state, the DuckDB sidecar on an ephemeral
port, then `wrangler dev --local` on 127.0.0.1:8794 (inspector 9244). `--local`
is a second safeguard: the top-level wrangler config has no `ai` binding, and
`--local` disables remote bindings even if one were added later.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import urllib.request

from peoplelens.dev import llm, procs
from peoplelens.dev.keys import ACCESS_AUD, ACCESS_ISSUER, ensure_keys
from peoplelens.paths import DEV_DIR, DUCKDB_PATH, POLICY_DIR, REPO_ROOT, WORKER_DIR

STATE_DIR = DEV_DIR / "wrangler-state"
DEV_VARS = WORKER_DIR / ".dev.vars"
BASE_URL = f"http://127.0.0.1:{procs.WRANGLER_PORT}"


def _health() -> dict | None:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def write_dev_vars(values: dict[str, str]) -> None:
    # Single-quoted dotenv values are literal, so JSON needs no escaping.
    for k, v in values.items():
        if "'" in v or "\n" in v:
            raise ValueError(f"{k} cannot be written to .dev.vars")
    lines = [f"{k}='{v}'" for k, v in values.items()]
    DEV_VARS.write_text("\n".join(lines) + "\n")
    os.chmod(DEV_VARS, 0o600)


def up(llm_provider: str = "stub", timeout: float = 90.0) -> int:
    if llm_provider not in ("stub", "openai_compat"):
        print("--llm must be stub or openai_compat (Workers AI runs only in production)")
        return 2
    if "wrangler" in procs.read_pids() or "sidecar" in procs.read_pids():
        print("the local stack is already up; run `uv run pl dev down` first")
        return 2
    for port in (procs.WRANGLER_PORT, procs.INSPECTOR_PORT):
        if not procs.port_free(port):
            print(f"port {port} is in use; stop whatever holds it first")
            return 2
    if not DUCKDB_PATH.exists():
        print("DuckDB warehouse missing; run `make dbt` first")
        return 2
    if llm_provider == "openai_compat" and not llm.healthy():
        print("llama-server is not running on 8180; run `make llm-up` first")
        return 2
    if shutil.which("npx") is None:
        print("npx not found; install Node 22 or newer")
        return 2

    _, jwks = ensure_keys()
    shutil.rmtree(STATE_DIR, ignore_errors=True)

    token = secrets.token_urlsafe(32)
    sidecar_env = dict(os.environ, PL_WAREHOUSE_TOKEN=token)
    sidecar = procs.start(
        "sidecar", [sys.executable, "-m", "peoplelens.cli", "warehouse", "serve", "--port", "0"],
        env=sidecar_env, stdout_pipe=True,
    )
    line = sidecar.stdout.readline().decode() if sidecar.stdout else ""
    try:
        port = int(json.loads(line)["listening"])
    except (ValueError, KeyError, json.JSONDecodeError):
        procs.record("sidecar", sidecar)
        procs.stop("sidecar")
        print("warehouse sidecar failed to start; see .dev/logs/sidecar.log")
        return 1
    procs.record("sidecar", sidecar, {"port": port})

    bindings = (POLICY_DIR / "role_bindings.local.json").read_text()
    dev_vars = {
        "ENVIRONMENT": "local",
        "LLM_PROVIDER": llm_provider,
        "WAREHOUSE": "duckdb_local",
        "PLANNER_MODEL": llm.MODEL_NAME if llm_provider == "openai_compat" else "stub-keyword-planner",
        "ACCESS_JWKS_JSON": json.dumps(jwks),
        "ACCESS_AUD": ACCESS_AUD,
        "ACCESS_ISSUER": ACCESS_ISSUER,
        "ROLE_BINDINGS_JSON": json.dumps(json.loads(bindings)),
        "WAREHOUSE_URL": f"http://127.0.0.1:{port}",
        "WAREHOUSE_TOKEN": token,
    }
    if llm_provider == "openai_compat":
        dev_vars["LLM_BASE_URL"] = llm.BASE_URL
    write_dev_vars(dev_vars)

    env = {k: v for k, v in os.environ.items() if k != "CLOUDFLARE_API_TOKEN"}
    env["WRANGLER_SEND_METRICS"] = "false"
    env.setdefault("CI", os.environ.get("CI", ""))
    wrangler = procs.start(
        "wrangler",
        ["npx", "wrangler", "dev", "--local", "--ip", "127.0.0.1", "--port", str(procs.WRANGLER_PORT),
         "--inspector-port", str(procs.INSPECTOR_PORT), "--persist-to", str(STATE_DIR), "--show-interactive-dev-session=false"],
        cwd=WORKER_DIR, env=env,
    )
    procs.record("wrangler", wrangler, {"port": procs.WRANGLER_PORT})
    if not procs.wait_for(lambda: _health() is not None, timeout, 0.5):
        print("wrangler dev did not answer /api/health; see .dev/logs/wrangler.log")
        down()
        return 1
    health = _health()
    print(json.dumps({"worker": BASE_URL, "health": health, "sidecar_port": port}))
    return 0 if health and health.get("ok") else 1


def down() -> int:
    recorded = procs.read_pids()
    groups = [recorded[n].get("pgid", recorded[n]["pid"]) for n in ("wrangler", "sidecar") if n in recorded]
    stopped = [name for name in ("wrangler", "sidecar") if procs.stop(name)]
    ok = procs.wait_for(lambda: procs.port_free(procs.WRANGLER_PORT) and procs.port_free(procs.INSPECTOR_PORT), 15)
    left = [g for g in groups if procs.group_alive(g)]
    if DEV_VARS.exists():
        DEV_VARS.unlink()
    status = "ok" if ok and not left else "ports still busy or processes left"
    print(json.dumps({"stopped": stopped, "ports_free": ok, "status": status}))
    return 0 if ok and not left else 1


def repo_relative(path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)
