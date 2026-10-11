"""Bootstrap phase B, steps 10 to 15: Cloudflare Access and identities.

Team domain from the Zero Trust organization; an Access application whose
destination is the Worker; an Allow policy for the four role emails and a
Service Auth policy for four eval service tokens; ROLE_BINDINGS_JSON; then a
redeploy with ACCESS_TEAM_DOMAIN, ACCESS_AUD and ROLE_BINDINGS_JSON.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from peoplelens.dev.client import USER_AGENT
from peoplelens.ops.bootstrap_cloudflare import BootstrapError, deploy
from peoplelens.ops.checks import PROD_EVAL_ENV, Plan, Runner, load_state, read_env_file, save_state, write_env_file
from peoplelens.ops.cloudflare_api import WORKER_NAME, CloudflareApi
from peoplelens.ops.roles import role_bindings

FALLBACK = (
    "Fallback (SPEC.md 17.4 step 5): Workers & Pages > peoplelens > Access > Protect this Worker behind Access > "
    "All traffic > choose a policy > Apply Access; copy the Application Audience (AUD) tag from Zero Trust > "
    "Access controls > Applications > Configure > Additional settings, then rerun "
    "`make bootstrap ARGS=\"--phase b --access-aud <tag> ...\"`."
)
ROLE_KEYS = {
    "people_analytics": "PEOPLE_ANALYTICS",
    "hr_business_partner": "HR_BUSINESS_PARTNER",
    "talent_acquisition": "TALENT_ACQUISITION",
    "executive": "EXECUTIVE",
}
MAX_BINDINGS_BYTES = 5 * 1024


def token_name(role: str) -> str:
    return f"peoplelens-eval-{role.replace('_', '-')}"


def ensure_service_tokens(api: CloudflareApi, plan: Plan, dry_run: bool) -> dict[str, dict[str, str]]:
    """One token per role. A token whose secret is not in prod_eval.env is rotated, not reused."""
    saved = read_env_file(PROD_EVAL_ENV)
    existing = {t["name"]: t for t in api.service_tokens()}
    out: dict[str, dict[str, str]] = {}
    for role, key in ROLE_KEYS.items():
        name = token_name(role)
        have_secret = bool(saved.get(f"{key}_CLIENT_SECRET")) and bool(saved.get(f"{key}_CLIENT_ID"))
        token = existing.get(name)
        if token and have_secret and saved[f"{key}_CLIENT_ID"] == token.get("client_id"):
            plan.add(f"service token {name} exists and its secret is saved")
            out[role] = {"id": token["id"], "client_id": token["client_id"], "client_secret": saved[f"{key}_CLIENT_SECRET"]}
            continue
        if dry_run:
            plan.add(f"service token {name}: {'rotate (secret not saved)' if token else 'create'}")
            out[role] = {"id": token["id"] if token else "", "client_id": token.get("client_id", "") if token else "", "client_secret": ""}
            continue
        if token:
            plan.add(f"rotate service token {name} (its secret is not in .secrets/prod_eval.env)")
            fresh = api.rotate_service_token(token["id"])
            out[role] = {"id": token["id"], "client_id": fresh.get("client_id", token["client_id"]), "client_secret": fresh["client_secret"]}
        else:
            plan.add(f"create service token {name}")
            fresh = api.create_service_token(name)
            out[role] = {"id": fresh["id"], "client_id": fresh["client_id"], "client_secret": fresh["client_secret"]}
    if not dry_run:
        values = {}
        for role, key in ROLE_KEYS.items():
            values[f"{key}_CLIENT_ID"] = out[role]["client_id"]
            values[f"{key}_CLIENT_SECRET"] = out[role]["client_secret"]
        write_env_file(PROD_EVAL_ENV, values)
    return out


def check_protected(url: str) -> str:
    """An unauthenticated request must be redirected to Access or refused."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        resp = opener.open(urllib.request.Request(f"{url}/api/me", headers={"user-agent": USER_AGENT}), timeout=15)
        return f"unexpected {resp.status}"
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("location", "")
        if exc.code in (301, 302, 303, 307) and "cloudflareaccess.com" in location:
            return "redirected to Access"
        if exc.code in (401, 403):
            return f"refused ({exc.code})"
        return f"unexpected {exc.code}"
    except urllib.error.URLError as exc:
        return f"unreachable ({exc.reason})"


def phase_b(api: CloudflareApi, emails: dict[str, dict], runner: Runner, plan: Plan, access_aud: str | None = None) -> None:
    state = load_state()
    if not state.get("worker_url"):
        raise BootstrapError("run phase A first (no Worker URL in .secrets/bootstrap-state.json)")

    # 10. team domain and login method
    org = api.organization()
    team = (org or {}).get("auth_domain")
    if not team:
        raise BootstrapError("no Zero Trust organization: complete onboarding and pick a team name (SPEC.md 17.4 step 3)")
    plan.add(f"Zero Trust team domain {team}")
    if not any(p.get("type") == "onetimepin" for p in api.identity_providers()):
        plan.add("WARN: no One-time PIN login method; add it in Zero Trust > Integrations > Identity providers > Add new identity provider > One-time PIN (17.4 step 4)")

    # 11. Access application protecting the Worker
    aud = access_aud
    app_id = state.get("access_app_id")
    if not aud:
        worker = api.worker(WORKER_NAME)
        if not worker or not worker.get("id"):
            raise BootstrapError(f"Worker {WORKER_NAME} not found; run phase A. {FALLBACK}")
        try:
            if runner.dry_run:
                plan.add("Access application peoplelens (destination: the Worker): get or create")
                app = next((a for a in api.access_apps() if a.get("name") == "peoplelens"), {"id": "", "aud": ""})
            else:
                action, app = api.ensure_access_app(worker["id"])
                plan.add(f"Access application peoplelens: {action}")
            app_id = app.get("id") or app.get("uid")
            aud = app.get("aud") or (api.access_app(app_id).get("aud") if app_id and not runner.dry_run else None)
        except Exception as exc:  # noqa: BLE001 - print the documented fallback on any API failure
            raise BootstrapError(f"creating the Access application failed ({exc}). {FALLBACK}") from exc
        if not aud and not runner.dry_run:
            raise BootstrapError(f"the Access application has no aud tag in the API response. {FALLBACK}")

    # 13. service tokens (before the policies that reference them)
    tokens = ensure_service_tokens(api, plan, runner.dry_run)

    # 12. policies on the application
    if app_id and not runner.dry_run:
        allow = [{"email": {"email": info["email"]}} for info in emails.values()]
        svc = [{"service_token": {"token_id": t["id"]}} for t in tokens.values()]
        a1, _ = api.ensure_policy(app_id, "peoplelens-users", "allow", allow, 1)
        a2, _ = api.ensure_policy(app_id, "peoplelens-eval-service-tokens", "non_identity", svc, 2)
        plan.add(f"Access policies: users {a1}, service tokens {a2}")
    else:
        plan.add("Access policies: Allow (four role emails), Service Auth (four service tokens)")

    # 14. role bindings
    bindings = role_bindings(emails, {r: t["client_id"] for r, t in tokens.items() if t["client_id"]})
    payload = json.dumps(bindings, separators=(",", ":"))
    if len(payload.encode()) > MAX_BINDINGS_BYTES:
        raise BootstrapError(f"ROLE_BINDINGS_JSON is {len(payload)} bytes; the variable limit is 5 KB")
    plan.add(f"ROLE_BINDINGS_JSON with {len(bindings)} bindings ({len(payload)} bytes)")

    # 15. redeploy with the Access secrets, then verify
    deploy(runner, plan, {"ACCESS_TEAM_DOMAIN": team, "ACCESS_AUD": aud or "", "ROLE_BINDINGS_JSON": payload})
    if not runner.dry_run:
        state = load_state()  # deploy() recorded deployed_commit; keep it
        state.update({"access_team_domain": team, "access_app_id": app_id, "access_aud": aud})
        save_state(state)
        plan.add(f"unauthenticated /api/me: {check_protected(state['worker_url'])}")
