"""Bootstrap phase A, steps 7 to 9 (workers.dev subdomain, AI Gateway, first
deploy), and `pl-ops deploy`.

The first deploy ships GCP_PROJECT_ID, AI_GATEWAY_ID and (when newly created)
GCP_SA_KEY_JSON through `wrangler deploy --env production --secrets-file`
(additive: secrets left out are kept). Until phase B sets the Access secrets the
Worker fails closed: every /api route answers 503.
"""

from __future__ import annotations

import json
import os

from peoplelens.ops.checks import DEPLOY_SECRETS, WORKER_KEY, Plan, Runner, load_state, save_state, write_private
from peoplelens.ops.cloudflare_api import GATEWAY_ID, WORKER_NAME, CloudflareApi
from peoplelens.paths import REPO_ROOT, WORKER_DIR


class BootstrapError(RuntimeError):
    pass


def worker_url(subdomain: str) -> str:
    return f"https://{WORKER_NAME}.{subdomain}.workers.dev"


def ensure_subdomain(api: CloudflareApi, requested: str | None, plan: Plan, dry_run: bool) -> str:
    current = api.subdomain()
    if current:
        plan.add(f"workers.dev subdomain is {current}")
        return current
    if not requested:
        raise BootstrapError(
            "this account has no workers.dev subdomain. Pick one in Workers & Pages, or rerun "
            "`make bootstrap ARGS=\"... --workers-subdomain <name>\"` (SPEC.md 17.4 step 2)."
        )
    plan.add(f"register workers.dev subdomain {requested}")
    return requested if dry_run else api.register_subdomain(requested)


def ensure_gateway(api: CloudflareApi, plan: Plan, dry_run: bool) -> None:
    if dry_run:
        existing = api.gateway(GATEWAY_ID)
        plan.add(f"AI Gateway {GATEWAY_ID}: {'converge settings' if existing else 'create'} (authentication on, metadata logs, 120/min)")
        return
    action, _ = api.ensure_gateway(GATEWAY_ID)
    plan.add(f"AI Gateway {GATEWAY_ID}: {action}")


def deploy(runner: Runner, plan: Plan, secrets: dict[str, str] | None = None) -> None:
    """`wrangler deploy --env production`, with a temporary secrets file when secrets are given.

    The web UI is rebuilt first (web/dist is gitignored and is the Worker's static-assets
    directory, so a fresh clone has none and an old build would ship a stale UI). Once
    the audit trail is enabled (`--with-audit` recorded audit_enabled), every deploy
    uses the generated config with the queue and R2 bucket bindings, so a later plain
    deploy cannot silently drop them.
    """
    build = runner.run(["npm", "run", "build:web"], cwd=REPO_ROOT, mutates=True)
    if not build.ok:
        raise BootstrapError(f"building the web UI failed (npm run build:web): {(build.stderr or build.stdout).strip()[-400:]}")
    cmd = ["npx", "wrangler", "deploy"]
    if load_state().get("audit_enabled"):
        from peoplelens.ops.bootstrap_audit import write_generated_config

        cmd += ["--config", write_generated_config(dry_run=runner.dry_run)]
        plan.add("audit trail enabled: deploying with the audit queue and the R2 staging bucket")
    cmd += ["--env", "production"]
    if secrets:
        write_private(DEPLOY_SECRETS, json.dumps(secrets))
        cmd += ["--secrets-file", str(DEPLOY_SECRETS)]
        plan.add(f"deploy Worker {WORKER_NAME} with secrets {', '.join(sorted(secrets))}")
    else:
        plan.add(f"deploy Worker {WORKER_NAME} (existing secrets kept)")
    try:
        res = runner.run(cmd, cwd=WORKER_DIR, mutates=True)
    finally:
        if DEPLOY_SECRETS.exists():
            DEPLOY_SECRETS.unlink()
    if not res.ok:
        raise BootstrapError(f"wrangler deploy failed: {(res.stderr or res.stdout).strip()[-600:]}")


def phase_a_cloudflare(api: CloudflareApi, project: str, runner: Runner, plan: Plan,
                       workers_subdomain: str | None) -> None:
    state = load_state()
    sub = ensure_subdomain(api, workers_subdomain, plan, runner.dry_run)
    ensure_gateway(api, plan, runner.dry_run)
    secrets = {"GCP_PROJECT_ID": project, "AI_GATEWAY_ID": GATEWAY_ID}
    if WORKER_KEY.exists():
        secrets["GCP_SA_KEY_JSON"] = json.dumps(json.loads(WORKER_KEY.read_text()))
    elif not state.get("worker_key_deployed") and not runner.dry_run:
        raise BootstrapError("no worker key to deploy; rerun phase A (it creates .secrets/worker-key.json)")
    deploy(runner, plan, secrets)
    if WORKER_KEY.exists() and not runner.dry_run:
        os.remove(WORKER_KEY)
        state["worker_key_deployed"] = True
    state.update({"workers_subdomain": sub, "worker_url": worker_url(sub), "ai_gateway_id": GATEWAY_ID})
    if not runner.dry_run:
        save_state(state)
    plan.add(f"Worker live at {worker_url(sub)}; /api answers 503 until phase B sets the Access secrets")
