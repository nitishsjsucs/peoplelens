"""Shared plumbing for ops: command runner, check results, secrets and state files."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from peoplelens.paths import REPO_ROOT, SECRETS_DIR

STATE_PATH = SECRETS_DIR / "bootstrap-state.json"
CLOUDFLARE_ENV = SECRETS_DIR / "cloudflare.env"
PROD_EVAL_ENV = SECRETS_DIR / "prod_eval.env"
EVAL_KEY = SECRETS_DIR / "peoplelens-eval.json"
WORKER_KEY = SECRETS_DIR / "worker-key.json"
DEPLOY_SECRETS = SECRETS_DIR / "deploy-secrets.json"

OK, MISSING, WARN, SKIP = "OK", "MISSING", "WARN", "SKIP"


@dataclass
class Check:
    group: str
    name: str
    status: str
    fix: str = ""
    detail: str = ""
    required: bool = True


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner:
    """Runs CLIs non-interactively (stdin is /dev/null unless input is given, so a CLI
    prompt cannot hang a captured run). wrangler never sees CLOUDFLARE_API_TOKEN, so it
    keeps using its OAuth login, but it is given the account id from
    .secrets/cloudflare.env: with several accounts on one login a non-interactive
    wrangler refuses to guess, and the deploy must land in the account the API token
    manages (AI Gateway, Access)."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.log: list[list[str]] = []

    def available(self, tool: str) -> bool:
        return shutil.which(tool) is not None

    def run(self, cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
            input_text: str | None = None, mutates: bool = False) -> Result:
        self.log.append(cmd)
        if self.dry_run and mutates:
            return Result(0, "", "")
        full_env = dict(os.environ if env is None else env)
        if cmd and (cmd[0] == "npx" or "wrangler" in cmd[:2]):
            full_env.pop("CLOUDFLARE_API_TOKEN", None)
            full_env.pop("CLOUDFLARE_ACCOUNT_ID", None)
            account = read_env_file(CLOUDFLARE_ENV).get("CLOUDFLARE_ACCOUNT_ID")
            if account:
                full_env["CLOUDFLARE_ACCOUNT_ID"] = account
            full_env.setdefault("WRANGLER_SEND_METRICS", "false")
        stdin = None if input_text is not None else subprocess.DEVNULL
        try:
            proc = subprocess.run(cmd, cwd=str(cwd or REPO_ROOT), env=full_env, input=input_text, stdin=stdin,
                                  capture_output=True, text=True, check=False, timeout=900)
        except FileNotFoundError:
            return Result(127, "", f"{cmd[0]}: not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", "timed out")
        return Result(proc.returncode, proc.stdout, proc.stderr)


def git_build_commit() -> str:
    """`git rev-parse HEAD`, with "-dirty" appended when the working tree has changes.

    Every deploy sends it as the BUILD_COMMIT var; /api/health reports it, so a
    production eval spanning several UTC days can check the deployed build never changed."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False).stdout.strip()

    head = git("rev-parse", "HEAD")
    if len(head) != 40:
        return "unknown"
    return head + ("-dirty" if git("status", "--porcelain") else "")


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def write_private(path: Path, text: str) -> None:
    """Write a secrets file with mode 0600 inside a 0700 directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def write_env_file(path: Path, values: dict[str, str]) -> None:
    write_private(path, "".join(f"{k}={v}\n" for k, v in values.items()))


def mode_too_open(path: Path) -> bool:
    return path.exists() and bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    write_private(STATE_PATH, json.dumps(state, indent=2, sort_keys=True) + "\n")


def render_table(checks: list[Check]) -> str:
    rows = [("GROUP", "CHECK", "STATUS", "FIX")]
    for c in checks:
        rows.append((c.group, c.name + (f" ({c.detail})" if c.detail else ""), c.status, c.fix if c.status != OK else ""))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    lines = []
    for r in rows:
        lines.append(" | ".join([r[0].ljust(widths[0]), r[1].ljust(widths[1]), r[2].ljust(widths[2]), r[3]]).rstrip())
    return "\n".join(lines)


@dataclass
class Plan:
    """What a bootstrap phase did or would do, for --dry-run and tests."""

    steps: list[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        self.steps.append(text)
        print(f"- {text}")
