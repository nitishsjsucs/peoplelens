"""`pl-ops doctor`: check every prerequisite and print exactly what is missing.

Output: GROUP | CHECK | STATUS (OK, MISSING, WARN, SKIP) | FIX, where FIX is the
exact command or console path (SPEC.md 17.2 to 17.6). Exits non-zero when a
required check in an applicable group fails. Governance never fails the run
unless --strict-governance is passed; the audit group runs only with --with-audit.

Production runs on the Cloudflare Workers Free plan (nothing needs Workers Paid, so
there is no plan check) in a Google Cloud project shared with three other repos, so
the gcp group also checks that no peoplelens-* identity holds a project-level role
other than roles/bigquery.jobUser and that peoplelens-* identities appear only, and
read-only, in the access lists of peoplelens_* datasets (SPEC.md 25.4 E).

Every run is read-only. "Exactly what is missing": checks that cannot run yet (they
need the Cloudflare API token) are still listed as SKIP with their fix; what the
bootstrap itself creates (APIs, the audit catalog and config) is a WARN until the
bootstrap has run and required afterwards; billing that is off says why (no account
linked, or the linked account is closed); fixes name the account id and project when
the logins show them; and the output ends with the missing required items in order.
"""

from __future__ import annotations

import json
import re
import shutil
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


def wrangler_accounts(runner: Runner) -> tuple[bool, list[str]]:
    """(logged in, account ids) from `npx wrangler whoami --json` (wrangler 4.149.0 exits
    non-zero when not authenticated). Falls back to the text output's wording."""
    who = runner.run(["npx", "wrangler", "whoami", "--json"], cwd=WORKER_DIR)
    try:
        data = json.loads(who.stdout)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        ids = [str(a.get("id")) for a in data.get("accounts") or [] if isinstance(a, dict) and a.get("id")]
        return bool(data.get("loggedIn")) and who.ok, ids
    text = (who.stdout + who.stderr).lower()
    return who.ok and "not authenticated" not in text and "logged in" in text, []


def token_fix(account_ids: list[str]) -> str:
    """The exact fix for a missing token file, naming the account id when the login sees one."""
    account = account_ids[0] if len(account_ids) == 1 else "<account-id from npx wrangler whoami>"
    return (f"create the peoplelens-bootstrap token (SPEC.md 17.3), then put CLOUDFLARE_ACCOUNT_ID={account} and "
            "CLOUDFLARE_API_TOKEN=<token> in .secrets/cloudflare.env (chmod 600)")


# Checks that need the API token. Without it they cannot run, but they are still listed
# (SKIP, with their fix) so the operator sees every step that is left, not only the first.
OTP_FIX = "Zero Trust > Integrations > Identity providers > Add new identity provider > One-time PIN (SPEC.md 17.4 step 4)"
ZERO_TRUST_FIX = "Zero Trust: complete onboarding, pick a team name, choose the Free plan (SPEC.md 17.4 step 3)"
SUBDOMAIN_FIX = "pick one in Workers & Pages, or pass --workers-subdomain <name> to make bootstrap"


def token_dependent_skips(g: str, why: str) -> list[Check]:
    return [
        Check(g, "API token permissions (7 reads, SPEC.md 17.7)", SKIP, "checked once the token works", why, required=False),
        Check(g, "Zero Trust organization", SKIP, ZERO_TRUST_FIX, why, required=False),
        Check(g, "One-time PIN login method", SKIP, OTP_FIX, why, required=False),
        Check(g, "workers.dev subdomain", SKIP, SUBDOMAIN_FIX, why, required=False),
    ]


def cloudflare_checks(runner: Runner, api: CloudflareApi | None) -> list[Check]:
    g = "cloudflare"
    out: list[Check] = []
    logged_in, account_ids = wrangler_accounts(runner)
    detail = f"{len(account_ids)} account{'s' if len(account_ids) != 1 else ''}" if account_ids else ""
    out.append(Check(g, "wrangler login (OAuth)", OK if logged_in else MISSING, "npx wrangler login", detail))
    if not CLOUDFLARE_ENV.exists():
        out.append(Check(g, ".secrets/cloudflare.env", MISSING, token_fix(account_ids)))
        return out + token_dependent_skips(g, "needs .secrets/cloudflare.env")
    out.append(Check(g, ".secrets/cloudflare.env mode 0600", WARN if mode_too_open(CLOUDFLARE_ENV) else OK, "chmod 600 .secrets/cloudflare.env", required=False))
    if api is None:
        out.append(Check(g, "account id and token in cloudflare.env", MISSING, token_fix(account_ids)))
        return out + token_dependent_skips(g, "needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN")
    if account_ids:
        # wrangler deploys with this account id (the ops runner passes it), so the OAuth
        # login must be able to see it; otherwise the Worker and the token's resources
        # (gateway, Access) would live in different accounts, or the deploy would fail.
        mine = api.account_id in account_ids
        out.append(Check(g, "CLOUDFLARE_ACCOUNT_ID is an account the wrangler login can deploy to", OK if mine else MISSING,
                         f"set CLOUDFLARE_ACCOUNT_ID to one of: {', '.join(account_ids)}, or npx wrangler login as a member of it"))
    try:
        tok = api.verify_token()
        status = tok.get("status")
        out.append(Check(g, "API token active", OK if status == "active" else MISSING, token_fix(account_ids),
                         f"expires {tok.get('expires_on') or 'never'}"))
    except CloudflareError as exc:
        out.append(Check(g, "API token active", MISSING, token_fix(account_ids), str(exc)[:80]))
        return out + token_dependent_skips(g, "needs a working API token")
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
                         ZERO_TRUST_FIX, (org or {}).get("auth_domain", "")))
        try:
            otp = any(p.get("type") == "onetimepin" for p in api.identity_providers())
            out.append(Check(g, "One-time PIN login method", OK if otp else MISSING, OTP_FIX))
        except CloudflareError as exc:
            out.append(Check(g, "One-time PIN login method", MISSING, "add Access: Organizations, Identity Providers, and Groups: Read", str(exc)[:60]))
    if "Workers Scripts (subdomain)" in results:
        sub = results["Workers Scripts (subdomain)"]
        out.append(Check(g, "workers.dev subdomain", OK if sub else MISSING, SUBDOMAIN_FIX, sub or ""))
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


def _json(text: str):
    try:
        return json.loads(text) if text and text.strip() else None
    except json.JSONDecodeError:
        return None


def _account_id(name: str) -> str:
    return (name or "").rsplit("/", 1)[-1]


BUDGET_FIX = "Console > Billing > Budgets and alerts > Create budget: scope the shared project, $5, alerts at 50, 90 and 100% (SPEC.md 17.5)"


def billing_checks(runner: Runner, project: str) -> list[Check]:
    """Billing enabled on the shared project, and why not: no account linked, or the linked
    account is closed (then linking cannot help until it is reopened or another is linked).
    With billing on, a budget on the billing account (WARN only: it guards cost, not function)."""
    g = "gcp"
    desc = runner.run(["gcloud", "billing", "projects", "describe", project, "--format=json"])
    info = _json(desc.stdout) if desc.ok else None
    if not isinstance(info, dict):
        return [Check(g, "billing enabled", MISSING, f"check that this login can read billing: gcloud billing projects describe {project}",
                      (desc.stderr or desc.stdout).strip()[:80])]
    linked = _account_id(info.get("billingAccountName", ""))
    if info.get("billingEnabled") is True:
        return [Check(g, "billing enabled", OK, detail=f"account {linked}"), budget_check(runner, project, linked)]
    accounts = _json(runner.run(["gcloud", "billing", "accounts", "list", "--format=json"]).stdout)
    accounts = accounts if isinstance(accounts, list) else []
    open_ids = [_account_id(a.get("name", "")) for a in accounts if isinstance(a, dict) and a.get("open") is True]
    target = open_ids[0] if len(open_ids) == 1 else "<open billing account id: gcloud billing accounts list>"
    link = f"gcloud billing projects link {project} --billing-account={target}"
    closed = linked and any(_account_id(a.get("name", "")) == linked and a.get("open") is False
                            for a in accounts if isinstance(a, dict))
    if closed:
        fix = (f"the linked billing account is closed: reopen it (Console > Billing > Manage billing accounts, clear the Active "
               f"filter > {linked} > Account management > Reopen billing account; needs Billing Account Administrator), "
               f"then rerun the doctor; or link an open one: {link}")
        detail = f"linked to {linked}, which is closed"
    elif not open_ids:
        fix = f"create or reopen a billing account (Console > Billing > Manage billing accounts), then {link}"
        detail = f"linked to {linked}" if linked else "no billing account linked"
    else:
        fix = link
        detail = f"linked to {linked}" if linked else "no billing account linked"
    return [Check(g, "billing enabled", MISSING, fix, detail),
            Check(g, "budget alert on the billing account", SKIP, BUDGET_FIX, "after billing is enabled", required=False)]


def budget_check(runner: Runner, project: str, account: str) -> Check:
    g = "gcp"
    name = "budget alert on the billing account"
    res = runner.run(["gcloud", "billing", "budgets", "list", f"--billing-account={account}", "--format=json"])
    budgets = _json(res.stdout) if res.ok else None
    if not isinstance(budgets, list):
        return Check(g, name, WARN, BUDGET_FIX, f"could not list budgets: {(res.stderr or res.stdout).strip()[:60]}", required=False)
    number = runner.run(["gcloud", "projects", "describe", project, "--format=value(projectNumber)"]).stdout.strip()
    covering = []
    for b in budgets:
        projects = ((b or {}).get("budgetFilter") or {}).get("projects") or []
        if not projects or (number and f"projects/{number}" in projects):
            covering.append((b or {}).get("displayName") or "unnamed")
    return Check(g, name, OK if covering else WARN, BUDGET_FIX, ", ".join(covering)[:60], required=False)


def phase_a_ran() -> bool:
    return bool(load_state().get("gcp_project"))


def dataset_isolation_check(runner: Runner, project: str, fast: bool) -> Check:
    """No peoplelens-* identity in another dataset's access list, and only read access on
    this repo's datasets: in the shared project every data grant is dataset-scoped
    (SPEC.md 25.4 E), so the access lists are where a leak would show."""
    g = "gcp"
    name = "shared project: peoplelens-* read only peoplelens_* datasets"
    if fast:
        return Check(g, name, SKIP, "run without --fast", required=False)
    if not runner.available("bq"):
        return Check(g, name, MISSING, "gcloud components install bq")
    listing = runner.run(["bq", "ls", f"--project_id={project}", "--format=json", "--max_results=1000"])
    if not listing.ok:
        return Check(g, name, MISSING, f"bq ls --project_id={project} (needs bigquery.datasets.get)", (listing.stderr or listing.stdout).strip()[:60])
    datasets = _json(listing.stdout) or []
    bad: list[str] = []
    for ds in datasets:
        ds_id = ((ds or {}).get("datasetReference") or {}).get("datasetId", "")
        if not ds_id:
            continue
        shown = _json(runner.run(["bq", "show", "--format=json", f"{project}:{ds_id}"]).stdout)
        if not isinstance(shown, dict):
            bad.append(f"{ds_id} unreadable")
            continue
        for entry in shown.get("access") or []:
            member = next((str(v) for k, v in (entry or {}).items() if k != "role" and isinstance(v, str) and "peoplelens-" in v), "")
            if not member:
                continue
            role = str((entry or {}).get("role", ""))
            if not ds_id.startswith("peoplelens_"):
                bad.append(f"{member.split('@')[0].split(':')[-1]} on {ds_id}")
            elif role not in ("READER", "roles/bigquery.dataViewer"):
                bad.append(f"{member.split('@')[0].split(':')[-1]} {role} on {ds_id}")
    fix = "revoke it (REVOKE ... ON SCHEMA, or bq update --source); this repo reads only its own datasets, read-only (SPEC.md 25.4 E)"
    return Check(g, name, OK if not bad else MISSING, fix, "; ".join(bad) if bad else f"{len(datasets)} datasets checked")


def gcp_checks(runner: Runner, project: str | None, fast: bool = False) -> list[Check]:
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
    out += billing_checks(runner, project)
    enabled = runner.run(["gcloud", "services", "list", "--enabled", f"--project={project}", "--format=value(config.name)"])
    names = set(enabled.stdout.split())
    missing = [a for a in APIS if a not in names]
    if missing and not phase_a_ran():
        # Not a prerequisite: bootstrap phase A step 1 enables them. Listed so nothing is a surprise.
        out.append(Check(g, "required APIs enabled", WARN, "nothing to do now: make bootstrap enables them (phase A step 1)",
                         ", ".join(missing), required=False))
    else:
        out.append(Check(g, "required APIs enabled", OK if not missing else MISSING,
                         "make bootstrap ARGS=\"--phase a ...\" (step 1 enables them)", ", ".join(missing)))
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
    out.append(dataset_isolation_check(runner, project, fast))
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


ROLE_EMAILS = "analyst=<you>+analyst@<domain>,hrbp=<you>+hrbp@<domain>:Engineering+Sales,recruiter=<you>+recruiter@<domain>,exec=<you>+exec@<domain>"


def bootstrap_command(project: str | None) -> str:
    """The exact bootstrap command once the prerequisites pass (SPEC.md 17.6)."""
    return f'make bootstrap ARGS="--gcp-project {project or "<shared-project-id>"} --role-emails {ROLE_EMAILS}"'


def deployed_checks(runner: Runner, api: CloudflareApi | None, project: str | None, http=_http) -> list[Check]:
    g = "deployed"
    state = load_state()
    project = project or state.get("gcp_project")
    if not state.get("worker_url"):
        return [Check(g, "bootstrap phases A and B (nothing deployed yet)", SKIP, bootstrap_command(project), required=False)]
    out: list[Check] = []
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


def audit_checks(runner: Runner, project: str | None = None, api: CloudflareApi | None = None) -> list[Check]:
    """Optional audit trail (SPEC.md 25.4 A). Prerequisites: the token can list queues and
    R2 buckets (Queues: Edit, Workers R2 Storage: Edit; R2 activated). Created by the
    bootstrap: Basin Catalog active on the bucket and the generated deploy config free of
    Basin Pipelines (required once `--with-audit` has run). For make audit-commit: the R2 keys."""
    from peoplelens.ops.bootstrap_audit import BUCKET, GENERATED_CONFIG, parse_catalog

    g = "audit"
    state = load_state()
    fix = f'make bootstrap ARGS="--gcp-project {project or state.get("gcp_project") or "<shared-project-id>"} --phase a --with-audit"'
    enabled = bool(state.get("audit_enabled"))
    out: list[Check] = []
    perm_fix = "add Queues: Edit and Workers R2 Storage: Edit to the peoplelens-bootstrap token (SPEC.md 17.3); R2 must be activated (card on file)"
    for name, path in (("Queues: Edit (list queues)", "/queues"), ("Workers R2 Storage: Edit (list buckets)", "/r2/buckets")):
        if api is None:
            out.append(Check(g, f"permission {name}", SKIP, perm_fix, "needs .secrets/cloudflare.env", required=False))
            continue
        try:
            api.result("GET", path)
            out.append(Check(g, f"permission {name}", OK))
        except CloudflareError as exc:
            out.append(Check(g, f"permission {name}", MISSING, perm_fix, str(exc)[:60]))
    later = "nothing to do now: " + fix + " creates it"
    info = parse_catalog(runner.run(["npx", "wrangler", "basin", "catalog", "get", BUCKET], cwd=WORKER_DIR).stdout)
    active = info.get("status", "").lower() == "active"
    out.append(Check(g, f"Basin Catalog active on {BUCKET}", OK if active else (MISSING if enabled else WARN), fix if enabled else later,
                     info.get("status", "not enabled"), required=enabled))
    if GENERATED_CONFIG.exists():
        text = GENERATED_CONFIG.read_text()
        out.append(Check(g, "generated config has no pipelines key (Workers Paid only)", MISSING if '"pipelines"' in text else OK, fix))
    else:
        out.append(Check(g, "generated config (worker/wrangler.audit.generated.jsonc)", MISSING if enabled else WARN,
                         fix if enabled else later, required=enabled))
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
        target = project or state.get("gcp_project") or "<shared-project-id>"
        return [Check(g, "phase C (policy tags, masking)", SKIP, f'make bootstrap ARGS="--phase c --gcp-project {target}" (optional)', required=False)]
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
        checks += gcp_checks(runner, project, fast)
    if "deployed" in groups:
        checks += deployed_checks(runner, api, project)
    if "audit" in groups:
        checks += audit_checks(runner, project, api)
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
          f"{sum(c.status == WARN for c in checks)} warnings, {sum(c.status == SKIP for c in checks)} skipped")
    if failed:
        print("\nMissing (required), in order:")
        for i, c in enumerate(failed, 1):
            print(f"{i}. {c.group}: {c.name}: {c.fix}")
    return 1 if failed else 0

