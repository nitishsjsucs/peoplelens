"""Bootstrap phase A, steps 1 to 6: Google Cloud (gcloud, bq, dbt-bigquery).

Every step is idempotent: APIs are enabled (a no-op when already on), datasets
use CREATE SCHEMA IF NOT EXISTS plus ALTER SCHEMA, service accounts are described
before they are created, IAM bindings and DCL grants are additive, and keys are
created only when absent. No write role is granted anywhere.
"""

from __future__ import annotations

import os

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
    "peoplelens-analyst": "PeopleLens masking demo (reads peoplelens_core)",
}
# Read-only roles; tests assert this list never gains a write role.
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


def _check(res, what: str) -> None:
    if not res.ok:
        raise BootstrapError(f"{what} failed: {(res.stderr or res.stdout).strip()[:400]}")


def phase_a_gcp(project: str, runner: Runner, plan: Plan) -> None:
    # 1. APIs
    plan.add(f"enable APIs on {project}: {', '.join(APIS)}")
    _check(runner.run(["gcloud", "services", "enable", *APIS, f"--project={project}"], mutates=True), "gcloud services enable")

    # 2. datasets (create, then converge options)
    plan.add("create or converge datasets peoplelens_raw, _staging, _core, _semantic (location US, time travel)")
    _check(
        runner.run(["bq", "query", f"--project_id={project}", "--use_legacy_sql=false", "--location=US"],
                   input_text=render_sql("01_datasets.sql", project), mutates=True),
        "dataset DDL",
    )

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
            _check(runner.run(["gcloud", "projects", "add-iam-policy-binding", project,
                               f"--member=serviceAccount:{sa_email(name, project)}", f"--role={role}",
                               "--condition=None", "--quiet"], mutates=True), f"bind {role} to {name}")
    plan.add("grant dataViewer on peoplelens_semantic to worker and eval, on peoplelens_core to analyst")
    _check(
        runner.run(["bq", "query", f"--project_id={project}", "--use_legacy_sql=false", "--location=US"],
                   input_text=render_sql("02_dataset_grants.sql", project), mutates=True),
        "dataset grants",
    )

    # 5. dbt-bigquery: seeds, staging, core, authorized semantic views (policy tags off)
    env = dict(os.environ, GCP_PROJECT_ID=project)
    for args in (["seed"], ["build"]):
        plan.add(f"pl dbt --dialect bigquery {' '.join(args)}")
        _check(runner.run(["uv", "run", "pl", "dbt", "--dialect", "bigquery", *args], env=env, mutates=True),
               f"dbt {' '.join(args)}")

    # 6. keys, only if absent
    state = load_state()
    if not state.get("worker_key_deployed") and not WORKER_KEY.exists():
        plan.add("create a key for peoplelens-worker (uploaded by the deploy step, then deleted locally)")
        _check(runner.run(["gcloud", "iam", "service-accounts", "keys", "create", str(WORKER_KEY),
                           f"--iam-account={sa_email('peoplelens-worker', project)}", f"--project={project}"],
                          mutates=True), "worker key")
    if not EVAL_KEY.exists():
        plan.add("create a key for peoplelens-eval (.secrets/peoplelens-eval.json)")
        _check(runner.run(["gcloud", "iam", "service-accounts", "keys", "create", str(EVAL_KEY),
                           f"--iam-account={sa_email('peoplelens-eval', project)}", f"--project={project}"],
                          mutates=True), "eval key")
        if EVAL_KEY.exists():
            os.chmod(EVAL_KEY, 0o600)
    state["gcp_project"] = project
    if not runner.dry_run:
        save_state(state)
