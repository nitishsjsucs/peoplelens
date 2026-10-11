"""`make bootstrap ARGS="--with-audit ..."`: the optional metadata-only audit trail in
production (below the core cut line), on the Workers Free plan (SPEC.md 25.4 A).

Worker -> Queue `peoplelens-audit` (one of this repo's 800 daily slots in
AccountBudgetDO; otherwise a direct put) -> the same Worker's queue() handler ->
immutable NDJSON objects in R2 bucket `peoplelens-audit` (staging/v1/...) ->
`make audit-commit` (Python, pyiceberg RestCatalog) -> Basin Catalog Iceberg table
`peoplelens.audit_events`. No Basin Pipelines resource is created: Basin Pipelines
is Workers Paid only (error 1021 on this account).

Create commands are not idempotent, so every resource is listed first. The deploy
uses a generated, gitignored config (worker/wrangler.audit.generated.jsonc) that adds
the queue producer and consumer and the R2 binding to env.production: a deploy that
binds a missing queue or bucket fails, so the bindings cannot live in the committed
config. The generated config never contains a `pipelines` key.
"""

from __future__ import annotations

import json
import re
import secrets as pysecrets

from peoplelens.ops.bootstrap_cloudflare import BootstrapError, deploy
from peoplelens.ops.checks import Plan, Runner, load_state, save_state
from peoplelens.ops.cloudflare_api import CloudflareApi
from peoplelens.paths import WORKER_DIR
from peoplelens.policy import load as load_policy

QUEUE = "peoplelens-audit"
BUCKET = "peoplelens-audit"
GENERATED_CONFIG = WORKER_DIR / "wrangler.audit.generated.jsonc"
_FREE = load_policy().limits["free_plan"]
# Production consumer: batches of up to 100 events, at most 30 s apart, 10 retries
# (60 s apart, set by the consumer's retryAll); no retention or delivery-delay setting
# (Free retention is fixed at 24 h).
CONSUMER = {
    "queue": QUEUE,
    "max_batch_size": _FREE["audit_batch_max"],
    "max_batch_timeout": _FREE["audit_batch_timeout_s"],
    "max_retries": _FREE["audit_max_retries"],
}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_jsonc(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def generated_config() -> dict:
    cfg = json.loads(strip_jsonc((WORKER_DIR / "wrangler.jsonc").read_text()))
    prod = cfg["env"]["production"]
    prod["queues"] = {"producers": [{"binding": "AUDIT_QUEUE", "queue": QUEUE}], "consumers": [dict(CONSUMER)]}
    prod["r2_buckets"] = [{"binding": "AUDIT_BUCKET", "bucket_name": BUCKET}]
    return cfg


def write_generated_config(dry_run: bool = False) -> str:
    """Write worker/wrangler.audit.generated.jsonc (gitignored); returns its file name."""
    if not dry_run:
        GENERATED_CONFIG.write_text(json.dumps(generated_config(), indent=2) + "\n")
    return GENERATED_CONFIG.name


def parse_catalog(text: str) -> dict[str, str]:
    """Catalog URI, Warehouse and Status from `wrangler basin catalog get` (labelled values)
    or `enable` (quoted values), as printed by wrangler 4.149.0."""
    clean = ANSI.sub("", text or "")
    out: dict[str, str] = {}
    for label, key in (("Catalog URI", "uri"), ("Warehouse", "warehouse"), ("Status", "status")):
        m = re.search(rf"^\s*{label}:\s*'?([^'\s]+)'?\s*$", clean, re.MULTILINE)
        if m:
            out[key] = m.group(1)
    return out


def _catalog_active(info: dict[str, str]) -> bool:
    return bool(info.get("uri")) and info.get("status", "").lower() == "active"


def phase_audit(api: CloudflareApi, runner: Runner, plan: Plan) -> None:
    state = load_state()

    queues = api.result("GET", "/queues") or []
    if not any(q.get("queue_name") == QUEUE for q in queues):
        plan.add(f"create queue {QUEUE} (no retention or delay flags: Free retention is 24 h)")
        res = runner.run(["npx", "wrangler", "queues", "create", QUEUE], cwd=WORKER_DIR, mutates=True)
        if not res.ok and not runner.dry_run:
            raise BootstrapError(f"queue create failed: {(res.stderr or res.stdout).strip()[:300]}")
    else:
        plan.add(f"queue {QUEUE} exists")
    buckets = (api.result("GET", "/r2/buckets") or {}).get("buckets", [])
    if not any(b.get("name") == BUCKET for b in buckets):
        plan.add(f"create R2 bucket {BUCKET}")
        res = runner.run(["npx", "wrangler", "r2", "bucket", "create", BUCKET], cwd=WORKER_DIR, mutates=True)
        if not res.ok and not runner.dry_run:
            raise BootstrapError(f"R2 bucket create failed: {(res.stderr or res.stdout).strip()[:300]}")
    else:
        plan.add(f"R2 bucket {BUCKET} exists")

    get_cmd = ["npx", "wrangler", "basin", "catalog", "get", BUCKET]
    info = parse_catalog(runner.run(get_cmd, cwd=WORKER_DIR).stdout)
    if _catalog_active(info):
        plan.add(f"Basin Catalog is active on {BUCKET}")
    else:
        plan.add(f"enable Basin Catalog on {BUCKET} (Free with an active R2 subscription; compaction stays off)")
        res = runner.run(["npx", "wrangler", "basin", "catalog", "enable", BUCKET], cwd=WORKER_DIR, mutates=True)
        if not res.ok and not runner.dry_run:
            raise BootstrapError(f"Basin Catalog enable failed: {(res.stderr or res.stdout).strip()[:300]}")
        info = parse_catalog(res.stdout) or parse_catalog(runner.run(get_cmd, cwd=WORKER_DIR).stdout)
    if info.get("uri") and info.get("warehouse"):
        state.update({"audit_catalog_uri": info["uri"], "audit_warehouse": info["warehouse"]})
        plan.add(f"recorded the Catalog URI and Warehouse of {BUCKET} in .secrets/bootstrap-state.json")
    elif not runner.dry_run:
        plan.add("could not read the Catalog URI and Warehouse; copy them from R2 > peoplelens-audit > Settings > "
                 "Basin Catalog and pass them to `make audit-commit ARGS=\"--catalog-uri <uri> --warehouse <name>\"`")

    if runner.dry_run:
        plan.add("write worker/wrangler.audit.generated.jsonc (queue producer and consumer, AUDIT_BUCKET) and deploy with AUDIT_HMAC_KEY")
        return
    # Recorded before the deploy: from now on every deploy (bootstrap or `make deploy`)
    # uses the generated config, so the audit bindings cannot be dropped by a later deploy.
    state["audit_enabled"] = True
    save_state(state)
    # The HMAC key is created once; rotating it would break subject continuity in the audit table.
    secrets = None if state.get("audit_hmac_key_set") else {"AUDIT_HMAC_KEY": pysecrets.token_urlsafe(32)}
    try:
        deploy(runner, plan, secrets)
    except BootstrapError as exc:
        raise BootstrapError(f"audit deploy failed: {exc}") from exc
    state = load_state()  # deploy() recorded deployed_commit; keep it
    state["audit_hmac_key_set"] = True
    save_state(state)
    plan.add("Worker redeployed with the audit queue, the R2 staging bucket and AUDIT_HMAC_KEY; commit staged events with `make audit-commit`")
