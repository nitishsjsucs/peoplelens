"""`make bootstrap ARGS="--with-audit ..."`: the optional metadata-only audit trail
in production (below the core cut line).

Worker -> Queue `peoplelens-audit` -> the same Worker's queue() handler ->
Basin Pipelines stream `peoplelens_audit` -> sink -> Basin Catalog (R2 Data
Catalog) Iceberg table `peoplelens.audit_events` in R2 bucket `peoplelens-audit`.

Create commands are not idempotent, so every resource is listed first. The
catalog token is read from .secrets/cloudflare.env (BASIN_CATALOG_TOKEN), never
typed on a command line by the user; it is briefly visible in the local process
list while wrangler runs because the CLI has no environment-variable form.
The stream id is account-specific, so the deploy uses a generated, gitignored
config (worker/wrangler.audit.generated.jsonc) that adds the queue and stream
bindings to env.production.
"""

from __future__ import annotations

import json
import secrets as pysecrets

from peoplelens.ops.bootstrap_cloudflare import BootstrapError
from peoplelens.ops.checks import CLOUDFLARE_ENV, DEPLOY_SECRETS, Plan, Runner, load_state, read_env_file, save_state, write_private
from peoplelens.ops.cloudflare_api import CloudflareApi
from peoplelens.paths import REPO_ROOT, WORKER_DIR

QUEUE = "peoplelens-audit"
BUCKET = "peoplelens-audit"
STREAM = "peoplelens_audit"
SINK = "peoplelens_audit_sink"
PIPELINE = "peoplelens_audit_pipeline"
SCHEMA_FILE = REPO_ROOT / "infra" / "cloudflare" / "audit_stream_schema.json"
GENERATED_CONFIG = WORKER_DIR / "wrangler.audit.generated.jsonc"


def _names(res, key: str = "name") -> dict[str, dict]:
    try:
        data = json.loads(res.stdout) if res.ok and res.stdout.strip() else []
    except json.JSONDecodeError:
        data = []
    if isinstance(data, dict):
        data = data.get("result") or data.get("items") or []
    return {d.get(key): d for d in data if isinstance(d, dict) and d.get(key)}


def strip_jsonc(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def generated_config(stream_id: str) -> dict:
    cfg = json.loads(strip_jsonc((WORKER_DIR / "wrangler.jsonc").read_text()))
    prod = cfg["env"]["production"]
    prod["queues"] = {
        "producers": [{"binding": "AUDIT_QUEUE", "queue": QUEUE}],
        "consumers": [{"queue": QUEUE, "max_batch_size": 50, "max_batch_timeout": 5}],
    }
    prod["pipelines"] = [{"binding": "AUDIT_STREAM", "stream": stream_id}]
    return cfg


def phase_audit(api: CloudflareApi, runner: Runner, plan: Plan) -> None:
    env = read_env_file(CLOUDFLARE_ENV)
    catalog_token = env.get("BASIN_CATALOG_TOKEN")
    if not catalog_token and not runner.dry_run:
        raise BootstrapError("add BASIN_CATALOG_TOKEN to .secrets/cloudflare.env (a dashboard-created catalog token)")
    state = load_state()

    queues = api.result("GET", "/queues") or []
    if not any(q.get("queue_name") == QUEUE for q in queues):
        plan.add(f"create queue {QUEUE}")
        runner.run(["npx", "wrangler", "queues", "create", QUEUE], cwd=WORKER_DIR, mutates=True)
    else:
        plan.add(f"queue {QUEUE} exists")
    buckets = (api.result("GET", "/r2/buckets") or {}).get("buckets", [])
    if not any(b.get("name") == BUCKET for b in buckets):
        plan.add(f"create R2 bucket {BUCKET}")
        runner.run(["npx", "wrangler", "r2", "bucket", "create", BUCKET], cwd=WORKER_DIR, mutates=True)
    else:
        plan.add(f"R2 bucket {BUCKET} exists")
    plan.add(f"enable Basin Catalog on {BUCKET}")
    runner.run(["npx", "wrangler", "basin", "catalog", "enable", BUCKET], cwd=WORKER_DIR, mutates=True)

    streams = _names(runner.run(["npx", "wrangler", "basin", "pipelines", "streams", "list", "--json"], cwd=WORKER_DIR))
    if STREAM not in streams:
        plan.add(f"create stream {STREAM} (schema {SCHEMA_FILE.name}, HTTP ingest off)")
        res = runner.run(["npx", "wrangler", "basin", "pipelines", "streams", "create", STREAM,
                          "--schema-file", str(SCHEMA_FILE), "--http-enabled", "false"], cwd=WORKER_DIR, mutates=True)
        streams = _names(runner.run(["npx", "wrangler", "basin", "pipelines", "streams", "list", "--json"], cwd=WORKER_DIR))
        if not res.ok and not runner.dry_run:
            raise BootstrapError(f"stream create failed: {res.stderr[:300]}")
    stream_id = (streams.get(STREAM) or {}).get("id", "")
    sinks = _names(runner.run(["npx", "wrangler", "basin", "pipelines", "sinks", "list", "--json"], cwd=WORKER_DIR))
    if SINK not in sinks:
        plan.add(f"create sink {SINK} -> Iceberg table peoplelens.audit_events")
        runner.run(["npx", "wrangler", "basin", "pipelines", "sinks", "create", SINK, "--type", "r2-data-catalog",
                    "--bucket", BUCKET, "--namespace", "peoplelens", "--table", "audit_events",
                    "--catalog-token", catalog_token or ""], cwd=WORKER_DIR, mutates=True)
    pipelines = _names(runner.run(["npx", "wrangler", "basin", "pipelines", "list", "--json"], cwd=WORKER_DIR))
    if PIPELINE not in pipelines:
        plan.add(f"create pipeline {PIPELINE}")
        runner.run(["npx", "wrangler", "basin", "pipelines", "create", PIPELINE, "--sql",
                    f"INSERT INTO {SINK} SELECT * FROM {STREAM}"], cwd=WORKER_DIR, mutates=True)

    if not stream_id and not runner.dry_run:
        raise BootstrapError("could not read the stream id from `wrangler basin pipelines streams list --json`")
    if runner.dry_run:
        plan.add("write worker/wrangler.audit.generated.jsonc and deploy with AUDIT_HMAC_KEY")
        return
    GENERATED_CONFIG.write_text(json.dumps(generated_config(stream_id), indent=2) + "\n")
    # The HMAC key is created once; rotating it would break subject continuity in the audit table.
    cmd = ["npx", "wrangler", "deploy", "--config", GENERATED_CONFIG.name, "--env", "production"]
    if not state.get("audit_hmac_key_set"):
        write_private(DEPLOY_SECRETS, json.dumps({"AUDIT_HMAC_KEY": pysecrets.token_urlsafe(32)}))
        cmd += ["--secrets-file", str(DEPLOY_SECRETS)]
    try:
        res = runner.run(cmd, cwd=WORKER_DIR, mutates=True)
    finally:
        DEPLOY_SECRETS.unlink(missing_ok=True)
    if not res.ok:
        raise BootstrapError(f"audit deploy failed: {(res.stderr or res.stdout)[-400:]}")
    state.update({"audit_stream_id": stream_id, "audit_hmac_key_set": True})
    save_state(state)
    plan.add("Worker redeployed with the audit queue, the stream binding and AUDIT_HMAC_KEY")

