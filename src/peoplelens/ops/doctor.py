"""`pl-ops doctor`: check every prerequisite and print exactly what is missing.

Output: GROUP | CHECK | STATUS (OK, MISSING, WARN, SKIP) | FIX, where FIX is the
exact command or console path (SPEC.md 17.2 to 17.6). Exits non-zero when a
required check in an applicable group fails. Governance never fails the run
unless --strict-governance is passed; the audit group runs only with --with-audit.

Production runs on the Cloudflare Workers Free plan (nothing needs Workers Paid, so
there is no plan check) in a Google Cloud project shared with three other repos, so
the gcp group also checks that no peoplelens-* identity holds a project-level role
other than roles/bigquery.jobUser (SPEC.md 25.4 E).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from peoplelens.dev.client import USER_AGENT
from peoplelens.ops.bootstrap_gcp import APIS, PROJECT_ROLES, SERVICE_ACCOUNTS, sa_email
from peoplelens.ops.checks import (
    CLOUDFLARE_ENV,
    EVAL_KEY,
    MISSING,
    OK,
    PROD_EVAL_ENV,
    SKIP,
    WARN,
    Check,
    Runner,
    load_state,
    mode_too_open,
    read_env_file,
    render_table,
)
from peoplelens.ops.cloudflare_api import GATEWAY_ID, CloudflareApi, CloudflareError
from peoplelens.paths import DUCKDB_PATH, SECRETS_DIR, WORKER_DIR

WRANGLER_VERSION = "4.149.0"
MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
PROD_SECRETS = ["GCP_PROJECT_ID", "GCP_SA_KEY_JSON", "AI_GATEWAY_ID", "ACCESS_TEAM_DOMAIN", "ACCESS_AUD", "ROLE_BINDINGS_JSON"]
TIME_TRAVEL = {"peoplelens_raw": 48, "peoplelens_staging": 168, "peoplelens_core": 168, "peoplelens_semantic": 168}
FIX_TOKEN = "Create the token (SPEC.md 17.3) and save CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in .secrets/cloudflare.env (chmod 600)"


def _version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x) for x in m.groups() if x is not None) if m else ()


def local_checks(runner: Runner, fast: bool = False) -> list[Check]:
    g = "local"
    out: list[Check] = []
    node = runner.run(["node", "--version"])
    out.append(Check(g, "node >= 22", OK if node.ok and _version(node.stdout) >= (22,) else MISSING,
                     "install Node 24 (https://nodejs.org or `brew install node`)", node.stdout.strip()))
    out.append(Check(g, "npm", OK if runner.run(["npm", "--version"]).ok else MISSING, "install npm with Node"))
    uv = runner.run(["uv", "--version"])
    out.append(Check(g, "uv", OK if uv.ok else MISSING, "brew install uv", uv.stdout.strip()))
    py = runner.run(["uv", "run", "python", "-c", "import sys; print(sys.version.split()[0])"])
    out.append(Check(g, "python 3.12 (uv)", OK if py.ok and py.stdout.startswith("3.12") else MISSING, "uv python install 3.12 && make setup", py.stdout.strip()))
    wr = runner.run(["npx", "wrangler", "--version"], cwd=WORKER_DIR)
    out.append(Check(g, f"wrangler {WRANGLER_VERSION}", OK if WRANGLER_VERSION in wr.stdout else MISSING, "make setup (npm ci)", wr.stdout.strip()[-20:]))
    from peoplelens.dev.procs import port_free

    for port in (8794, 9244, 8180):
        out.append(Check(g, f"port {port} free", OK if port_free(port) else WARN, "make dev-down / make llm-down, or stop whatever holds it", required=False))
    from peoplelens.dev.llm import model_path

    out.append(Check(g, "llama-server", OK if shutil.which("llama-server") else WARN, "brew install llama.cpp (only for make eval-local)", required=False))
    out.append(Check(g, "local model file", OK if model_path().exists() else WARN, "set PL_LLM_MODEL to a Qwen3-1.7B GGUF (only for make eval-local)", required=False))
    out.append(Check(g, "DuckDB warehouse built", OK if DUCKDB_PATH.exists() else MISSING, "make data && make dbt"))
    if fast:
        out.append(Check(g, "catalogs fresh", SKIP, "uv run pl catalog build --check", required=False))
    else:
        fresh = runner.run(["uv", "run", "pl", "catalog", "build", "--check"])
        out.append(Check(g, "catalogs fresh", OK if fresh.ok else MISSING, "make catalog (stop the local stack first: make dev-down)"))
    return out


def cloudflare_api_from_env() -> CloudflareApi | None:
    env = read_env_file(CLOUDFLARE_ENV)
    if env.get("CLOUDFLARE_ACCOUNT_ID") and env.get("CLOUDFLARE_API_TOKEN"):
        return CloudflareApi(env["CLOUDFLARE_ACCOUNT_ID"], env["CLOUDFLARE_API_TOKEN"])
    return None


def cloudflare_checks(runner: Runner, api: CloudflareApi | None) -> list[Check]:
    g = "cloudflare"
    out: list[Check] = []
    who = runner.run(["npx", "wrangler", "whoami"], cwd=WORKER_DIR)
    logged_in = who.ok and "not authenticated" not in (who.stdout + who.stderr).lower()
    out.append(Check(g, "wrangler login (OAuth)", OK if logged_in else MISSING, "npx wrangler login"))
    if not CLOUDFLARE_ENV.exists():
        out.append(Check(g, ".secrets/cloudflare.env", MISSING, FIX_TOKEN))
        return out
    out.append(Check(g, ".secrets/cloudflare.env mode 0600", WARN if mode_too_open(CLOUDFLARE_ENV) else OK, "chmod 600 .secrets/cloudflare.env", required=False))
    if api is None:
        out.append(Check(g, "account id and token in cloudflare.env", MISSING, FIX_TOKEN))
        return out
    try:
        tok = api.verify_token()
        status = tok.get("status")
        out.append(Check(g, "API token active", OK if status == "active" else MISSING, FIX_TOKEN,
                         f"expires {tok.get('expires_on') or 'never'}"))
    except CloudflareError as exc:
        out.append(Check(g, "API token active", MISSING, FIX_TOKEN, str(exc)[:80]))
        return out
    probes = [
        ("Account Settings: Read", lambda: api.account()),
        ("AI Gateway: Edit (list gateways)", lambda: api.list_gateways()),
        ("Workers AI: Read (model search)", lambda: api.model_available(MODEL)),
        ("Access: Apps and Policies (list apps)", lambda: api.access_apps()),
        ("Access: Service Tokens (list)", lambda: api.service_tokens()),
        ("Access: Organizations (read)", lambda: api.organization()),
        ("Workers Scripts (subdomain)", lambda: api.subdomain()),
    ]
    results = {}
    for name, fn in probes:
        try:
            results[name] = fn()
            out.append(Check(g, f"permission {name}", OK))
        except CloudflareError as exc:
            out.append(Check(g, f"permission {name}", MISSING, f"add this permission to the peoplelens-bootstrap token (SPEC.md 17.3)", str(exc)[:60]))
    if results.get("Workers AI: Read (model search)") is False:
        out.append(Check(g, f"model {MODEL}", MISSING, "check the model name in the Workers AI catalog"))
    org = results.get("Access: Organizations (read)")
    if "Access: Organizations (read)" in results:
        out.append(Check(g, "Zero Trust organization", OK if org and org.get("auth_domain") else MISSING,
                         "Zero Trust: complete onboarding, pick a team name, choose the Free plan (SPEC.md 17.4 step 3)",
                         (org or {}).get("auth_domain", "")))
        try:
            otp = any(p.get("type") == "onetimepin" for p in api.identity_providers())
            out.append(Check(g, "One-time PIN login method", OK if otp else MISSING,
                             "Zero Trust > Settings > Authentication > Login methods > Add new > One-time PIN"))
        except CloudflareError as exc:
            out.append(Check(g, "One-time PIN login method", MISSING, "add Access: Organizations, Identity Providers, and Groups: Read", str(exc)[:60]))
    if "Workers Scripts (subdomain)" in results:
        sub = results["Workers Scripts (subdomain)"]
        out.append(Check(g, "workers.dev subdomain", OK if sub else MISSING,
                         "pick one in Workers & Pages, or pass --workers-subdomain <name> to make bootstrap", sub or ""))
    return out


ALLOWED_PROJECT_ROLES = sorted({r for roles in PROJECT_ROLES.values() for r in roles})


def project_role_violations(policy: dict) -> list[str]:
    """`member role` pairs where a peoplelens-* service account holds a project-level role
    other than roles/bigquery.jobUser (the shared project's isolation rule)."""
    out = []
    for binding in policy.get("bindings") or []:
        role = binding.get("role", "")
        for member in binding.get("members") or []:
            name = member.split(":", 1)[-1]
            if name.startswith("peoplelens-") and role not in ALLOWED_PROJECT_ROLES:
                out.append(f"{name} {role}")
    return sorted(out)


def gcp_checks(runner: Runner, project: str | None) -> list[Check]:
    g = "gcp"
    out: list[Check] = []
    if not runner.available("gcloud"):
        out.append(Check(g, "gcloud CLI", MISSING, "brew install --cask gcloud-cli"))
        return out
    out.append(Check(g, "gcloud CLI", OK))
    out.append(Check(g, "bq CLI", OK if runner.available("bq") else MISSING, "gcloud components install bq"))
    acct = runner.run(["gcloud", "config", "get-value", "account"])
    out.append(Check(g, "active gcloud account", OK if acct.ok and acct.stdout.strip() else MISSING, "gcloud auth login", acct.stdout.strip()))
    adc = runner.run(["gcloud", "auth", "application-default", "print-access-token"])
    out.append(Check(g, "application default credentials", OK if adc.ok else MISSING, "gcloud auth application-default login"))
    cfg = runner.run(["gcloud", "config", "get-value", "project"]).stdout.strip()
    project = project or cfg or None
    if not project:
        out.append(Check(g, "project set", MISSING, "gcloud config set project <shared-project-id> (SPEC.md 17.5)"))
        return out
    out.append(Check(g, "project set", OK, detail=project))
    bill = runner.run(["gcloud", "billing", "projects", "describe", project, "--format=value(billingEnabled)"])
    out.append(Check(g, "billing enabled", OK if bill.stdout.strip().lower() == "true" else MISSING,
                     f"gcloud billing projects link {project} --billing-account=<BILLING_ID>"))
    enabled = runner.run(["gcloud", "services", "list", "--enabled", f"--project={project}", "--format=value(config.name)"])
    names = set(enabled.stdout.split())
    missing = [a for a in APIS if a not in names]
    out.append(Check(g, "required APIs enabled", OK if not missing else MISSING,
                     "make bootstrap (phase A enables them)", ", ".join(missing)))
    pol = runner.run(["gcloud", "resource-manager", "org-policies", "describe",
                      "iam.managed.disableServiceAccountKeyCreation", f"--project={project}", "--effective", "--format=json"])
    blocked = pol.ok and '"enforce": true' in pol.stdout.replace("True", "true")
    out.append(Check(g, "service-account key creation allowed", WARN if blocked or not pol.ok else OK,
                     "if your project sits under an organization, allow key creation for this project (SPEC.md 17.5)", required=False))
    iam = runner.run(["gcloud", "projects", "get-iam-policy", project, "--format=json"])
    name = "shared project: peoplelens-* hold no project role but roles/bigquery.jobUser"
    try:
        policy = json.loads(iam.stdout) if iam.ok else None
    except json.JSONDecodeError:
        policy = None
    if policy is None:
        out.append(Check(g, name, MISSING, f"gcloud projects get-iam-policy {project} (needs resourcemanager.projects.getIamPolicy)",
                         (iam.stderr or "").strip()[:60]))
    else:
        bad = project_role_violations(policy)
        fix = "remove it: gcloud projects remove-iam-policy-binding <project> --member=serviceAccount:<sa> --role=<role> (grant data access per dataset instead)"
        out.append(Check(g, name, OK if not bad else MISSING, fix, "; ".join(bad)))
    return out


def _http(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict, str]:
    req = urllib.request.Request(url)
    req.add_header("user-agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None

    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {}), ""
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        return exc.code, body, exc.headers.get("location", "")
    except urllib.error.URLError as exc:
        return 0, {}, str(exc.reason)


def deployed_checks(runner: Runner, api: CloudflareApi | None, project: str | None, http=_http) -> list[Check]:
    g = "deployed"
    state = load_state()
    if not state.get("worker_url"):
        return [Check(g, "bootstrap phase A", SKIP, "make bootstrap ARGS=\"--gcp-project <p> --role-emails ...\"", required=False)]
    out: list[Check] = []
    project = project or state.get("gcp_project")
    if project and runner.available("bq"):
        for ds, hours in TIME_TRAVEL.items():
            r = runner.run(["bq", "show", "--format=json", f"{project}:{ds}"])
            got = None
            if r.ok:
                try:
                    got = int(json.loads(r.stdout).get("maxTimeTravelHours", 168))
                except (ValueError, json.JSONDecodeError):
                    got = None
            out.append(Check(g, f"dataset {ds} (time travel {hours} h)", OK if got == hours else MISSING, "make bootstrap ARGS=\"--phase a ...\"", f"got {got}"))
        for name in SERVICE_ACCOUNTS:
            r = runner.run(["gcloud", "iam", "service-accounts", "describe", sa_email(name, project), f"--project={project}"])
            out.append(Check(g, f"service account {name}", OK if r.ok else MISSING, "make bootstrap ARGS=\"--phase a ...\""))
    if api is not None:
        try:
            gw = api.gateway(GATEWAY_ID)
            good = bool(gw) and gw.get("authentication") is True and gw.get("collect_logs") is True
            form = state.get("gateway_settings_form")
            out.append(Check(g, "AI Gateway peoplelens (authenticated, logging)", OK if good else MISSING, "make bootstrap ARGS=\"--phase a ...\"",
                             f"accepted settings: {form}" if form else ""))
        except CloudflareError as exc:
            out.append(Check(g, "AI Gateway peoplelens", MISSING, "add AI Gateway: Edit to the token", str(exc)[:60]))
    secrets = runner.run(["npx", "wrangler", "secret", "list", "--env", "production", "--format", "json"], cwd=WORKER_DIR)
    names = set()
    if secrets.ok:
        try:
            names = {s["name"] for s in json.loads(secrets.stdout)}
        except (json.JSONDecodeError, KeyError, TypeError):
            names = set()
    missing = [n for n in PROD_SECRETS if n not in names]
    out.append(Check(g, "Worker secrets", OK if not missing else MISSING,
                     "make bootstrap (phase A sets GCP_*, AI_GATEWAY_ID; phase B sets ACCESS_*, ROLE_BINDINGS_JSON)", ", ".join(missing)))
    url = state["worker_url"]
    tokens = read_env_file(PROD_EVAL_ENV)
    svc = {}
    if tokens.get("EXECUTIVE_CLIENT_ID"):
        svc = {"CF-Access-Client-Id": tokens["EXECUTIVE_CLIENT_ID"], "CF-Access-Client-Secret": tokens["EXECUTIVE_CLIENT_SECRET"]}
    status, body, _ = http(f"{url}/api/health", svc)
    if status == 200:
        out.append(Check(g, "/api/health", OK))
        deployed = state.get("deployed_commit")
        build = body.get("build_commit")
        if deployed:
            out.append(Check(g, "/api/health build_commit is the deployed commit", OK if build == deployed else MISSING,
                             "make deploy (from a clean tree)", f"{build} vs {deployed}"))
        else:
            out.append(Check(g, "/api/health build_commit", OK if build else WARN, "make deploy", str(build), required=False))
    elif status == 503:
        out.append(Check(g, "/api/health", MISSING, "make bootstrap ARGS=\"--phase b ...\"", f"503, missing {', '.join(body.get('missing', []))}"))
    else:
        out.append(Check(g, "/api/health", MISSING, "check the deploy (make deploy) and Access (phase B)", f"HTTP {status}"))
    status, _, location = http(f"{url}/api/me")
    protected = (status in (301, 302, 303, 307) and "cloudflareaccess.com" in location) or status in (401, 403)
    out.append(Check(g, "unauthenticated /api/me blocked by Access", OK if protected else MISSING,
                     "make bootstrap ARGS=\"--phase b ...\" (or the 17.4 step 5 dashboard fallback)", f"HTTP {status}"))
    if svc:
        status, body, _ = http(f"{url}/api/me", svc)
        out.append(Check(g, "service token resolves to its role", OK if status == 200 and body.get("role") == "executive" else MISSING,
                         "make bootstrap ARGS=\"--phase b ...\"", f"HTTP {status} {body.get('role', '')}"))
        budget = body.get("ai_budget") if status == 200 else None
        has_budget = isinstance(budget, dict) and isinstance(budget.get("cap"), int)
        out.append(Check(g, "/api/me reports this repo's Workers AI share (ai_budget)", OK if has_budget else MISSING, "make deploy",
                         f"{budget.get('used')} of {budget.get('cap')} neurons used today" if has_budget else ""))
    else:
        out.append(Check(g, "eval service tokens saved", MISSING, "make bootstrap ARGS=\"--phase b ...\" (writes .secrets/prod_eval.env)"))
    out.append(Check(g, "eval service-account key", OK if EVAL_KEY.exists() else MISSING, "make bootstrap ARGS=\"--phase a ...\""))
    if EVAL_KEY.exists() and mode_too_open(EVAL_KEY):
        out.append(Check(g, "eval key mode 0600", WARN, f"chmod 600 {EVAL_KEY}", required=False))
    return out


def audit_checks(runner: Runner) -> list[Check]:
    """Optional audit trail (SPEC.md 25.4 A): Basin Catalog active on the bucket, the
    generated deploy config free of Basin Pipelines, and the committer's R2 keys."""
    from peoplelens.ops.bootstrap_audit import BUCKET, GENERATED_CONFIG, parse_catalog

    g = "audit"
    fix = 'make bootstrap ARGS="--gcp-project <p> --phase a --with-audit"'
    info = parse_catalog(runner.run(["npx", "wrangler", "basin", "catalog", "get", BUCKET], cwd=WORKER_DIR).stdout)
    active = info.get("status", "").lower() == "active"
    out = [Check(g, f"Basin Catalog active on {BUCKET}", OK if active else MISSING, fix, info.get("status", "not enabled"))]
    if GENERATED_CONFIG.exists():
        text = GENERATED_CONFIG.read_text()
        out.append(Check(g, "generated config has no pipelines key (Workers Paid only)", MISSING if '"pipelines"' in text else OK, fix))
    else:
        out.append(Check(g, "generated config (worker/wrangler.audit.generated.jsonc)", MISSING, fix))
    env = read_env_file(CLOUDFLARE_ENV)
    keys = ("AUDIT_R2_TOKEN", "AUDIT_R2_ACCESS_KEY_ID", "AUDIT_R2_SECRET_ACCESS_KEY")
    missing = [k for k in keys if not env.get(k)]
    out.append(Check(g, "R2 API token for make audit-commit", OK if not missing else WARN,
                     "R2 > Manage R2 API Tokens > Admin Read & Write; add the three AUDIT_R2_* lines to .secrets/cloudflare.env (SPEC.md 17.3)",
                     ", ".join(missing), required=False))
    return out


def governance_checks(runner: Runner, project: str | None) -> list[Check]:
    g = "governance"
    state = load_state()
    tags = state.get("policy_tags") or {}
    if not tags:
        return [Check(g, "phase C (policy tags, masking)", SKIP, "make bootstrap ARGS=\"--phase c --gcp-project <p>\"", required=False)]
    out = [Check(g, "taxonomy and three policy tags", OK if len(tags) == 3 else MISSING, "make bootstrap ARGS=\"--phase c ...\"", required=False)]
    project = project or state.get("gcp_project")
    if project and runner.available("bq"):
        tagged = 0
        for table in ("peoplelens_raw.raw_employees", "peoplelens_raw.raw_candidates",
                      "peoplelens_raw.raw_compensation_history", "peoplelens_core.fct_workforce_monthly"):
            r = runner.run(["bq", "show", "--schema", "--format=json", f"{project}:{table}"])
            if r.ok:
                try:
                    tagged += sum(1 for f in json.loads(r.stdout) if f.get("policyTags", {}).get("names"))
                except json.JSONDecodeError:
                    pass
        out.append(Check(g, "policy tags on the eight columns", OK if tagged == 8 else MISSING, "make bootstrap ARGS=\"--phase c ...\"", f"{tagged} tagged", required=False))
    return out


def run(groups: list[str], project: str | None, strict_governance: bool, fast: bool, runner: Runner | None = None) -> int:
    runner = runner or Runner()
    api = cloudflare_api_from_env()
    checks: list[Check] = []
    if "local" in groups:
        checks += local_checks(runner, fast)
    if "cloudflare" in groups:
        checks += cloudflare_checks(runner, api)
    if "gcp" in groups:
        checks += gcp_checks(runner, project)
    if "deployed" in groups:
        checks += deployed_checks(runner, api, project)
    if "audit" in groups:
        checks += audit_checks(runner)
    if "governance" in groups:
        gov = governance_checks(runner, project)
        if strict_governance:
            for c in gov:
                c.required = True
        checks += gov
    if SECRETS_DIR.exists() and mode_too_open(Path(SECRETS_DIR)):
        checks.append(Check("local", ".secrets/ mode 0700", WARN, "chmod 700 .secrets", required=False))
    print(render_table(checks))
    failed = [c for c in checks if c.required and c.status == MISSING]
    print(f"\n{len(checks)} checks: {sum(c.status == OK for c in checks)} OK, {len(failed)} missing (required), "
          f"{sum(c.status == WARN for c in checks)} warnings, {sum(c.status == SKIP for c in checks)} skipped", file=sys.stderr)
    return 1 if failed else 0

