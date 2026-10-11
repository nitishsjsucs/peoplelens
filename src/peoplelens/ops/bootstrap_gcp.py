"""Bootstrap phase A, steps 1 to 6: Google Cloud (gcloud, bq, dbt-bigquery).

Every step is idempotent: APIs are enabled (a no-op when already on), datasets
use CREATE SCHEMA IF NOT EXISTS plus ALTER SCHEMA, service accounts are described
before they are created, IAM bindings and DCL grants are additive, and keys are
created only when absent. No write role is granted anywhere.
"""

from __future__ import annotations

import json
import os
import re
import time

from peoplelens.ops.checks import EVAL_KEY, WORKER_KEY, Plan, Runner, load_state, save_state
from peoplelens.paths import REPO_ROOT

GOVERNANCE_DIR = REPO_ROOT / "warehouse" / "bigquery" / "governance"
APIS = [
    "bigquery.googleapis.com",
    "bigquerydatapolicy.googleapis.com",
    "datacatalog.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudbilling.googleapis.com",
]
SERVICE_ACCOUNTS = {
    "peoplelens-worker": "PeopleLens Worker (reads peoplelens_semantic)",
    "peoplelens-eval": "PeopleLens eval baseline (reads peoplelens_semantic)",
    "peoplelens-analyst": "PeopleLens masking demo (reads peoplelens_core after phase C)",
}
# The only project-level role in the shared project (job creation cannot be granted per
# dataset, and the role reads no data); every data grant is dataset- or resource-scoped.
# Tests assert this never gains another role; doctor checks the live IAM policy.
PROJECT_ROLES = {
    "peoplelens-worker": ["roles/bigquery.jobUser"],
    "peoplelens-eval": ["roles/bigquery.jobUser"],
    "peoplelens-analyst": ["roles/bigquery.jobUser"],
}


class BootstrapError(RuntimeError):
    pass


def sa_email(name: str, project: str) -> str:
    return f"{name}@{project}.iam.gserviceaccount.com"


def render_sql(name: str, project: str) -> str:
    return (GOVERNANCE_DIR / name).read_text().replace("{project}", project)


# Shared Google Cloud project (SPEC.md 25.4 E): no job runs uncapped. Governance DDL and
# DCL bill nothing; the cap and the label are there so every job is bounded and attributable.
GOVERNANCE_MAX_BYTES = 100 * 1024 * 1024
JOB_LABEL = "app:peoplelens"


def bq_script(name: str, project: str) -> list[str]:
    """`bq` command for one governance SQL script, in the documented form
    `bq [global flags] query [query flags] 'SQL'`. The SQL is passed as the query
    argument (not on stdin) with comment lines removed, so the argument never starts
    with `--` and cannot be read as a flag."""
    sql = "\n".join(line for line in render_sql(name, project).splitlines() if not line.lstrip().startswith("--")).strip()
    return ["bq", f"--project_id={project}", "--location=US", "query", "--use_legacy_sql=false",
            f"--maximum_bytes_billed={GOVERNANCE_MAX_BYTES}", f"--label={JOB_LABEL}", sql]


def _check(res, what: str) -> None:
    if not res.ok:
        raise BootstrapError(f"{what} failed: {(res.stderr or res.stdout).strip()[:400]}")


# A new service account can take a minute or more to become visible to IAM and to
# BigQuery DCL; grants issued right after `service-accounts create` may fail with
# "does not exist" until then.
PROPAGATION = re.compile(r"does not exist|not found", re.IGNORECASE)
RETRY_ATTEMPTS = 8
RETRY_WAIT_S = 15.0


def _run_after_propagation(runner: Runner, cmd: list[str], what: str, *, sleep=time.sleep) -> None:
    for attempt in range(RETRY_ATTEMPTS):
        res = runner.run(cmd, mutates=True)
        if res.ok:
            return
        if attempt == RETRY_ATTEMPTS - 1 or not PROPAGATION.search(res.stderr + res.stdout):
            _check(res, what)
        print(f"  {what}: the new service account is not visible yet; retrying in {RETRY_WAIT_S:.0f} s")
        sleep(RETRY_WAIT_S)


def phase_a_gcp(project: str, runner: Runner, plan: Plan) -> None:
    # 1. APIs
    plan.add(f"enable APIs on {project}: {', '.join(APIS)}")
    _check(runner.run(["gcloud", "services", "enable", *APIS, f"--project={project}"], mutates=True), "gcloud services enable")

    # 2. datasets (create, then converge options)
    plan.add("create or converge datasets peoplelens_raw, _staging, _core, _semantic (location US, time travel)")
    _check(runner.run(bq_script("01_datasets.sql", project), mutates=True), "dataset DDL")

    # 3. service accounts (describe before create)
    for name, display in SERVICE_ACCOUNTS.items():
        email = sa_email(name, project)
        exists = runner.run(["gcloud", "iam", "service-accounts", "describe", email, f"--project={project}"]).ok
        if exists:
            plan.add(f"service account {name} exists")
            continue
        plan.add(f"create service account {name}")
        _check(runner.run(["gcloud", "iam", "service-accounts", "create", name, f"--project={project}",
                           f"--display-name={display}"], mutates=True), f"create {name}")

    # 4. IAM: project job user, dataset read grants (no write role anywhere)
    for name, roles in PROJECT_ROLES.items():
        for role in roles:
            plan.add(f"grant {role} on the project to {name}")
            _run_after_propagation(runner, ["gcloud", "projects", "add-iam-policy-binding", project,
                                            f"--member=serviceAccount:{sa_email(name, project)}", f"--role={role}",
                                            "--condition=None", "--quiet"], f"bind {role} to {name}")
    plan.add("grant dataViewer on peoplelens_semantic to worker and eval (the analyst gets peoplelens_core in phase C)")
    _run_after_propagation(runner, bq_script("02_dataset_grants.sql", project), "dataset grants")

    # 5. dbt-bigquery: seeds, staging, core, authorized semantic views. Before phase C the
    # policy tags are off. After it, a seed or build without the phase C vars would recreate
    # the tables untagged (dbt-bigquery drops a seed before loading it and replaces table
    # models), removing column security and masking, so a rerun passes the same vars.
    from peoplelens.ops.bootstrap_governance import policy_tag_vars

    tag_vars = policy_tag_vars(load_state().get("policy_tags"))
    extra = ["--vars", json.dumps(tag_vars, sort_keys=True)] if tag_vars else []
    env = dict(os.environ, GCP_PROJECT_ID=project)
    for args in (["seed"], ["build"]):
        plan.add(f"pl dbt --dialect bigquery {' '.join(args)}" + (" (keeps the phase C policy tags)" if tag_vars else ""))
        _check(runner.run(["uv", "run", "pl", "dbt", "--dialect", "bigquery", *args, *extra], env=env, mutates=True),
               f"dbt {' '.join(args)}")

    # 6. keys, only if absent (gcloud writes them into .secrets/, which must exist, mode 0700)
    state = load_state()
    if not runner.dry_run:
        WORKER_KEY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not state.get("worker_key_deployed") and not WORKER_KEY.exists():
        plan.add("create a key for peoplelens-worker (uploaded by the deploy step, then deleted locally)")
        _run_after_propagation(runner, ["gcloud", "iam", "service-accounts", "keys", "create", str(WORKER_KEY),
                                        f"--iam-account={sa_email('peoplelens-worker', project)}", f"--project={project}"],
                               "worker key")
        if WORKER_KEY.exists():
            os.chmod(WORKER_KEY, 0o600)
    if not EVAL_KEY.exists():
        plan.add("create a key for peoplelens-eval (.secrets/peoplelens-eval.json)")
        _run_after_propagation(runner, ["gcloud", "iam", "service-accounts", "keys", "create", str(EVAL_KEY),
                                        f"--iam-account={sa_email('peoplelens-eval', project)}", f"--project={project}"],
                               "eval key")
        if EVAL_KEY.exists():
            os.chmod(EVAL_KEY, 0o600)
    state["gcp_project"] = project
    if not runner.dry_run:
        save_state(state)
