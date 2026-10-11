"""Pin the Workers Free plan budgets and the shared Google Cloud project rules (SPEC.md
section 13 and 25.5). Production runs on the Workers Free plan in a Cloudflare account
and a Google Cloud project that three other repos share; changing a cap or a share
means changing this test, on purpose."""

from __future__ import annotations

import json
import math
import re
import subprocess

from peoplelens.eval.baseline import SQL_SCHEMA
from peoplelens.eval.baseline import build_messages as baseline_messages
from peoplelens.eval.generate import QUESTIONS_PATH, load_items
from peoplelens.ops import bootstrap_access, bootstrap_audit, bootstrap_gcp, cloudflare_api, drills, prod_eval, smoke
from peoplelens.ops.bootstrap_audit import generated_config, strip_jsonc
from peoplelens.paths import DBT_PROJECT_DIR, REPO_ROOT, WORKER_DIR
from peoplelens.policy import load
from peoplelens.warehouse import audit_sink

FREE = load().limits["free_plan"]
PLANNER_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
RATES = FREE["model_neurons_per_m_tokens"][PLANNER_MODEL]
# Workers Paid features and settings no production config may use (SPEC.md 1 and 10.1).
PAID_KEYS = {"pipelines", "tail_consumers", "streaming_tail_consumers", "logpush", "limits"}
# Queue settings this repo never sets: Free retention is fixed at 24 h, and nothing is delayed.
QUEUE_RETENTION_KEYS = {"message_retention_period", "message_retention_period_secs", "delivery_delay"}
# Workers AI models that bill on Workers Paid only (pricing page and 2026-07-28 changelog; SPEC.md 25.1).
PAID_BILLING_MODELS = {"kimi-k2.6", "kimi-k2.7-code", "glm-5.2", "glm-5.3", "glm-5.3-flash", "deepseek-v4-flash-0731", "deepseek-v4-pro-0813"}
ACCOUNT_QUEUE_OPS_PER_DAY = 10_000
# Template tokens per chat message (role header and end marker) for the token bound below.
TEMPLATE_TOKENS_PER_MESSAGE = 5


def neurons(tokens_in: float, tokens_out: float) -> float:
    return (tokens_in * RATES["input"] + tokens_out * RATES["output"]) / 1_000_000


def all_keys(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in all_keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in all_keys(v)}
    return set()


def wrangler_config() -> dict:
    return json.loads(strip_jsonc((WORKER_DIR / "wrangler.jsonc").read_text()))


def items() -> list[dict]:
    return load_items(QUESTIONS_PATH)


# ---------------------------------------------------------------- no Workers Paid feature


def test_no_wrangler_config_uses_a_workers_paid_feature():
    for cfg in (wrangler_config(), generated_config()):
        assert not all_keys(cfg) & PAID_KEYS, all_keys(cfg) & PAID_KEYS
        for migration in cfg["migrations"]:
            assert set(migration) == {"tag", "new_sqlite_classes"}, migration  # SQLite: the only DO backend on Free
        for env in (cfg, cfg["env"]["production"]):
            queues = env.get("queues") or {}
            for q in queues.get("producers", []) + queues.get("consumers", []):
                assert not set(q) & QUEUE_RETENTION_KEYS, q


def test_the_planner_model_does_not_need_workers_paid():
    for env in (wrangler_config()["env"]["production"],):
        model = env["vars"]["PLANNER_MODEL"]
        assert model == PLANNER_MODEL
        assert model.rsplit("/", 1)[-1] not in PAID_BILLING_MODELS
        assert model in FREE["model_neurons_per_m_tokens"]
    assert prod_eval.MODEL == PLANNER_MODEL


def test_no_code_or_config_uses_basin_pipelines():
    files = subprocess.run(["git", "ls-files", "src", "worker/src", "worker/wrangler.jsonc", "Makefile", ".github", "infra"],
                           cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.split()
    assert "infra/cloudflare/audit_stream_schema.json" not in files
    for name in files:
        path = REPO_ROOT / name
        if not path.exists():  # deleted in the working tree
            continue
        text = path.read_text(errors="replace")
        assert "AUDIT_STREAM" not in text, name
        assert "basin pipelines" not in text, name  # the CLI command form
        assert '"basin", "pipelines"' not in text, name
        assert "BASIN_CATALOG_TOKEN" not in text, name


def test_doctor_never_asks_for_workers_paid():
    """No check named for a paid plan and no fix that sends anyone to upgrade (test_doctor.py
    also runs the cloudflare group and finds no check mentioning a paid plan)."""
    source = (REPO_ROOT / "src" / "peoplelens" / "ops" / "doctor.py").read_text()
    assert not re.search(r'Check\(\s*g\s*,\s*"Workers Paid', source)
    assert "Plans > Workers Paid" not in source and "upgrade" not in source.lower()


# ---------------------------------------------------------------- Workers AI neurons


def test_neuron_shares_leave_the_other_repos_their_part():
    account = FREE["account_neurons_per_day"]
    assert account == 10_000
    # On an eval day this repo uses at most 8,000 of the account's 10,000 (the Worker's share plus the REST baseline).
    assert FREE["worker_neurons_per_day"] + FREE["eval_baseline_neurons_per_day"] <= 0.8 * account
    # The eval never takes the interactive reserve out of the Worker's share.
    assert FREE["eval_semantic_neurons_per_day"] + FREE["interactive_reserve_neurons"] <= FREE["worker_neurons_per_day"]
    assert FREE["planner_reserve_neurons"] <= FREE["interactive_reserve_neurons"]


def test_baseline_reservation_covers_the_largest_baseline_prompt():
    """Token bound without a tokenizer: (prompt characters + JSON schema characters) / 3 plus
    5 template tokens per message, and max_tokens (400) out, at the pinned rates."""
    worst = 0.0
    for item in items():
        if item["slice"] not in ("answerable", "adversarial"):
            continue
        for turn in item["turns"]:
            for dialect in ("bigquery", "duckdb"):
                msgs = baseline_messages(turn["text"], dialect)
                chars = sum(len(m["content"]) for m in msgs) + len(json.dumps(SQL_SCHEMA))
                tokens_in = chars / 3 + TEMPLATE_TOKENS_PER_MESSAGE * (len(msgs) + 1)
                worst = max(worst, neurons(tokens_in, 400))
    assert 120 < worst <= FREE["baseline_reserve_neurons"], worst


def test_a_full_production_eval_fits_in_at_most_12_utc_days():
    its = items()
    planner_turns = sum(len(i["turns"]) for i in its)  # upper bound: 13 of these are screened and never reach the LLM
    baseline_turns = sum(len(i["turns"]) for i in its if i["slice"] in ("answerable", "adversarial"))
    assert (planner_turns, baseline_turns) == (214, 184)
    semantic_days = math.ceil(planner_turns * FREE["planner_reserve_neurons"] / FREE["eval_semantic_neurons_per_day"])
    baseline_days = math.ceil(baseline_turns * FREE["baseline_reserve_neurons"] / FREE["eval_baseline_neurons_per_day"])
    assert max(semantic_days, baseline_days) <= 12, (semantic_days, baseline_days)
    # Typical usage (SPEC.md 25.5: 109.7 per planner call, 50.6 per baseline call) takes about 5 days.
    assert math.ceil(201 * 109.7 / FREE["eval_semantic_neurons_per_day"]) == 5
    assert math.ceil(184 * 50.6 / FREE["eval_baseline_neurons_per_day"]) == 5


def test_smoke_step_4_fits_the_workers_share():
    assert smoke.SMOKE_PLANNER_CALLS * FREE["planner_reserve_neurons"] <= smoke.SMOKE_MIN_NEURONS + FREE["planner_reserve_neurons"]
    assert smoke.SMOKE_MIN_NEURONS <= FREE["worker_neurons_per_day"]


# ---------------------------------------------------------------- Queues


def test_audit_queue_share_and_consumer_settings():
    assert FREE["audit_queue_messages_per_day"] * FREE["queue_ops_per_message"] <= 0.25 * ACCOUNT_QUEUE_OPS_PER_DAY
    prod = generated_config()["env"]["production"]
    (consumer,) = prod["queues"]["consumers"]
    assert consumer["max_batch_size"] == FREE["audit_batch_max"] <= 100
    assert consumer["max_batch_timeout"] == FREE["audit_batch_timeout_s"] <= 60
    assert consumer["max_retries"] == FREE["audit_max_retries"] <= 10
    (local,) = wrangler_config()["queues"]["consumers"]
    assert local["max_batch_size"] <= 100 and local["max_batch_timeout"] <= 60
    assert prod["r2_buckets"] == [{"binding": "AUDIT_BUCKET", "bucket_name": "peoplelens-audit"}]


def test_subrequest_bound_matches_the_worker():
    worker = (WORKER_DIR / "src" / "warehouse" / "bigquery.ts").read_text()
    assert "bq_max_polls_per_query" in worker
    # JWKS 1 + Google token 1 + dry run 1 + jobs.query 4 (main and 3 probes) + polls 4 x 3 (freePlan.test.ts measures it)
    assert 1 + 1 + 1 + 4 + 4 * FREE["bq_max_polls_per_query"] == FREE["max_external_subrequests"] <= 50


# ---------------------------------------------------------------- shared Google Cloud project and Cloudflare account


def test_every_resource_this_repo_creates_is_prefixed():
    names = [
        *bootstrap_gcp.SERVICE_ACCOUNTS,
        bootstrap_audit.QUEUE, bootstrap_audit.BUCKET,
        cloudflare_api.GATEWAY_ID, cloudflare_api.WORKER_NAME, cloudflare_api.APP_NAME,
        *(bootstrap_access.token_name(r) for r in ("people_analytics", "hr_business_partner", "talent_acquisition", "executive")),
        "peoplelens-users", "peoplelens-eval-service-tokens",
    ]
    assert all(n.startswith("peoplelens") for n in names), names
    datasets = re.findall(r"`\{project\}`\.(\w+)", (REPO_ROOT / "warehouse" / "bigquery" / "governance" / "01_datasets.sql").read_text())
    assert set(datasets) == {"peoplelens_raw", "peoplelens_staging", "peoplelens_core", "peoplelens_semantic"}
    taxonomy = json.loads((REPO_ROOT / "warehouse" / "bigquery" / "governance" / "taxonomy.json").read_text())
    assert taxonomy["taxonomy"]["displayName"] == "peoplelens"
    governance = (REPO_ROOT / "src" / "peoplelens" / "ops" / "bootstrap_governance.py").read_text()
    assert 'policy_id = f"peoplelens_{' in governance
    assert audit_sink.NAMESPACE == "peoplelens"
    drill = (REPO_ROOT / "src" / "peoplelens" / "ops" / "drills.py").read_text()
    assert "peoplelens_core.drill_dim_month_" in drill


def test_the_only_project_level_role_is_bigquery_job_user():
    assert {r for roles in bootstrap_gcp.PROJECT_ROLES.values() for r in roles} == {"roles/bigquery.jobUser"}
    grants = (REPO_ROOT / "warehouse" / "bigquery" / "governance" / "02_dataset_grants.sql").read_text()
    assert re.findall(r"ON SCHEMA `\{project\}`\.(\w+)", grants) and "ON PROJECT" not in grants.upper()


def test_every_bigquery_job_the_ops_code_builds_is_capped():
    import yaml

    src = REPO_ROOT / "src" / "peoplelens"
    configs = 0
    for path in src.rglob("*.py"):
        text = path.read_text()
        for m in re.finditer(r"QueryJobConfig\(", text):
            configs += 1
            window = text[m.start(): m.start() + 500]
            assert "maximum_bytes_billed" in window, f"{path.name}: a QueryJobConfig without maximum_bytes_billed"
    assert configs >= 2  # smoke and parity entries, and the baseline executor
    assert prod_eval.BASELINE_MAX_BYTES == prod_eval.PARITY_MAX_BYTES == 200 * 1024 * 1024
    assert drills.DRILL_MAX_BYTES == bootstrap_gcp.GOVERNANCE_MAX_BYTES == 100 * 1024 * 1024
    for script in ("01_datasets.sql", "02_dataset_grants.sql"):
        assert f"--maximum_bytes_billed={100 * 1024 * 1024}" in bootstrap_gcp.bq_script(script, "shared-project-123")
    profile = yaml.safe_load((DBT_PROJECT_DIR / "profiles" / "bigquery" / "profiles.yml").read_text())
    assert profile["peoplelens"]["outputs"]["bigquery"]["maximum_bytes_billed"] == 1024 ** 3
    # The Worker caps every job at the role's per-query cap (100 or 200 MiB).
    caps = load().limits["per_query_byte_cap"]
    assert max(caps.values()) == 200 * 1024 * 1024 and min(caps.values()) == 100 * 1024 * 1024
    assert "maximumBytesBilled" in (WORKER_DIR / "src" / "warehouse" / "bigquery.ts").read_text()
