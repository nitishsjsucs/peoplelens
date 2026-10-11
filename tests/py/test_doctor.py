"""Doctor: every group reports what is missing with the exact fix."""

from __future__ import annotations

import json

import pytest

from peoplelens.ops import checks, doctor
from peoplelens.ops.checks import MISSING, OK, SKIP, WARN, Result, Runner
from peoplelens.ops.cloudflare_api import CloudflareError


class ScriptRunner(Runner):
    def __init__(self, results: dict[str, Result], tools: set[str] | None = None):
        super().__init__()
        self.results = results
        self.tools = tools if tools is not None else {"gcloud", "bq"}

    def available(self, tool):
        return tool in self.tools

    def run(self, cmd, **kw):
        key = " ".join(cmd)
        for prefix, res in self.results.items():
            if key.startswith(prefix):
                return res
        return Result(1, "", "unscripted")


def by_name(checks_):
    return {c.name: c for c in checks_}


def test_local_versions(monkeypatch):
    r = ScriptRunner({
        "node --version": Result(0, "v20.11.0"),
        "npm --version": Result(0, "10"),
        "uv --version": Result(0, "uv 0.12.17"),
        "uv run python": Result(0, "3.12.13"),
        "npx wrangler --version": Result(0, "4.149.0"),
    })
    c = by_name(doctor.local_checks(r, fast=True))
    assert c["node >= 22"].status == MISSING
    assert c["python 3.12 (uv)"].status == OK
    assert c["wrangler 4.149.0"].status == OK
    assert c["catalogs fresh"].status == SKIP


class FakeCf:
    def __init__(self, status="active", fail=(), org=True, otp=True, sub="lumen", account_id="acc"):
        self.status, self.fail, self.org, self.otp, self.sub = status, set(fail), org, otp, sub
        self.account_id = account_id

    def _maybe(self, name, value):
        if name in self.fail:
            raise CloudflareError(403, "Authentication error")
        return value

    def verify_token(self):
        return {"status": self.status, "expires_on": None}

    def account(self):
        return self._maybe("account", {"id": "acc"})

    def list_gateways(self):
        return self._maybe("gateways", [])

    def model_available(self, m):
        return self._maybe("ai", True)

    def access_apps(self):
        return self._maybe("apps", [])

    def service_tokens(self):
        return self._maybe("tokens", [])

    def organization(self):
        return self._maybe("org", {"auth_domain": "lumen.cloudflareaccess.com"} if self.org else None)

    def identity_providers(self):
        return [{"type": "onetimepin"}] if self.otp else []

    def subdomain(self):
        return self._maybe("sub", self.sub)


@pytest.fixture()
def cf_env(tmp_path, monkeypatch):
    path = tmp_path / "cloudflare.env"
    path.write_text("CLOUDFLARE_ACCOUNT_ID=acc\nCLOUDFLARE_API_TOKEN=tok\n")
    path.chmod(0o600)
    monkeypatch.setattr(doctor, "CLOUDFLARE_ENV", path)
    return path


def test_cloudflare_reports_missing_permissions_and_console_steps(cf_env):
    r = ScriptRunner({"npx wrangler whoami": Result(0, "You are logged in")})
    c = by_name(doctor.cloudflare_checks(r, FakeCf(fail={"apps"}, org=False, otp=False, sub=None)))
    assert c["wrangler login (OAuth)"].status == OK
    assert c["API token active"].status == OK
    assert c["permission Access: Apps and Policies (list apps)"].status == MISSING
    assert "17.3" in c["permission Access: Apps and Policies (list apps)"].fix
    assert c["Zero Trust organization"].status == MISSING
    assert c["One-time PIN login method"].status == MISSING
    assert c["workers.dev subdomain"].status == MISSING
    # Production runs on Workers Free: nothing to check about a paid plan (SPEC.md 25).
    assert not any("paid" in name.lower() for name in c)


def whoami(*account_ids, logged_in=True):
    return Result(0 if logged_in else 1, json.dumps({"loggedIn": logged_in, "authType": "OAuth Token",
                                                     "accounts": [{"id": a, "name": f"{a}'s Account"} for a in account_ids]}))


def test_cloudflare_without_token_file(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "CLOUDFLARE_ENV", tmp_path / "missing.env")
    r = ScriptRunner({"npx wrangler whoami": Result(1, "You are not authenticated")})
    c = by_name(doctor.cloudflare_checks(r, None))
    assert c["wrangler login (OAuth)"].status == MISSING and c["wrangler login (OAuth)"].fix == "npx wrangler login"
    assert c[".secrets/cloudflare.env"].status == MISSING


def test_cloudflare_without_token_names_the_account_and_every_step_left(tmp_path, monkeypatch):
    """Before the token exists the doctor still lists the console steps it cannot check yet
    (One-time PIN above all), and the fix names the one account the wrangler login sees."""
    monkeypatch.setattr(doctor, "CLOUDFLARE_ENV", tmp_path / "missing.env")
    acc = "d" * 32
    out = doctor.cloudflare_checks(ScriptRunner({"npx wrangler whoami --json": whoami(acc)}), None)
    c = by_name(out)
    assert c["wrangler login (OAuth)"].status == OK and c["wrangler login (OAuth)"].detail == "1 account"
    assert c[".secrets/cloudflare.env"].status == MISSING and c[".secrets/cloudflare.env"].required
    assert f"CLOUDFLARE_ACCOUNT_ID={acc}" in c[".secrets/cloudflare.env"].fix
    for name in ("One-time PIN login method", "Zero Trust organization", "workers.dev subdomain", "API token permissions (7 reads, SPEC.md 17.7)"):
        assert c[name].status == SKIP and not c[name].required and c[name].fix, name
    assert c["One-time PIN login method"].fix.startswith("Zero Trust > Integrations > Identity providers")
    two = by_name(doctor.cloudflare_checks(ScriptRunner({"npx wrangler whoami --json": whoami("a" * 32, "b" * 32)}), None))
    assert "<account-id from npx wrangler whoami>" in two[".secrets/cloudflare.env"].fix
    out_logged_out = by_name(doctor.cloudflare_checks(ScriptRunner({"npx wrangler whoami --json": whoami(logged_in=False)}), None))
    assert out_logged_out["wrangler login (OAuth)"].status == MISSING


def test_cloudflare_account_id_must_be_one_the_wrangler_login_sees(cf_env):
    good = by_name(doctor.cloudflare_checks(ScriptRunner({"npx wrangler whoami --json": whoami("acc", "other")}), FakeCf()))
    name = "CLOUDFLARE_ACCOUNT_ID is an account the wrangler login can deploy to"
    assert good[name].status == OK and good["One-time PIN login method"].status == OK
    bad = by_name(doctor.cloudflare_checks(ScriptRunner({"npx wrangler whoami --json": whoami("other")}), FakeCf()))
    assert bad[name].status == MISSING and "other" in bad[name].fix


def test_cloudflare_token_rejected_still_lists_the_steps_left(cf_env):
    class Rejected(FakeCf):
        def verify_token(self):
            raise CloudflareError(401, "Invalid API Token")

    c = by_name(doctor.cloudflare_checks(ScriptRunner({"npx wrangler whoami --json": whoami("acc")}), Rejected()))
    assert c["API token active"].status == MISSING
    assert c["One-time PIN login method"].status == SKIP and c["One-time PIN login method"].detail == "needs a working API token"


BILLING_ON = Result(0, json.dumps({"billingAccountName": "billingAccounts/0A-1B-2C", "billingEnabled": True}))


def test_gcp_group(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "STATE_PATH", tmp_path / "state.json")
    r = ScriptRunner({
        "gcloud config get-value account": Result(0, "me@x.example"),
        "gcloud auth application-default print-access-token": Result(1, "", "no ADC"),
        "gcloud config get-value project": Result(0, "peoplelens-abc123"),
        "gcloud billing projects describe": BILLING_ON,
        "gcloud services list": Result(0, "bigquery.googleapis.com\niam.googleapis.com\n"),
        "gcloud resource-manager org-policies describe": Result(1, "", "no org"),
        "bq ls": Result(0, ""),
    })
    c = by_name(doctor.gcp_checks(r, None))
    assert c["active gcloud account"].status == OK
    assert c["application default credentials"].status == MISSING
    assert c["application default credentials"].fix == "gcloud auth application-default login"
    assert c["billing enabled"].status == OK and c["billing enabled"].detail == "account 0A-1B-2C"
    # Before bootstrap phase A, disabled APIs are listed but are not a prerequisite (phase A enables them).
    apis = c["required APIs enabled"]
    assert apis.status == WARN and not apis.required and "datacatalog" in apis.detail and "phase A" in apis.fix
    checks.save_state({"gcp_project": "peoplelens-abc123"})
    after = by_name(doctor.gcp_checks(r, None))["required APIs enabled"]
    assert after.status == MISSING and after.required
    no_gcloud = by_name(doctor.gcp_checks(ScriptRunner({}, tools=set()), None))
    assert no_gcloud["gcloud CLI"].fix == "brew install --cask gcloud-cli"


GCP_BASE = {
    "gcloud config get-value account": Result(0, "me@x.example"),
    "gcloud auth application-default print-access-token": Result(0, "ya29"),
    "gcloud config get-value project": Result(0, "shared-project-123"),
    "bq ls": Result(0, ""),
}


def test_billing_names_why_it_is_off():
    """A project linked to a closed billing account cannot be fixed by linking it again:
    the doctor says the account is closed and how to reopen it, or which open one to link."""
    closed = {"billingAccountName": "billingAccounts/AAA-111", "billingEnabled": False}
    accounts = [{"name": "billingAccounts/AAA-111", "open": False, "displayName": "My Billing Account"}]
    c = by_name(doctor.billing_checks(ScriptRunner({
        "gcloud billing projects describe": Result(0, json.dumps(closed)),
        "gcloud billing accounts list": Result(0, json.dumps(accounts)),
    }), "shared-project-123"))
    b = c["billing enabled"]
    assert b.status == MISSING and b.detail == "linked to AAA-111, which is closed"
    assert "Reopen billing account" in b.fix and "AAA-111" in b.fix
    assert c["budget alert on the billing account"].status == SKIP
    # One open account and nothing linked: the exact link command.
    one_open = accounts + [{"name": "billingAccounts/BBB-222", "open": True}]
    c = by_name(doctor.billing_checks(ScriptRunner({
        "gcloud billing projects describe": Result(0, json.dumps({"billingAccountName": "", "billingEnabled": False})),
        "gcloud billing accounts list": Result(0, json.dumps(one_open)),
    }), "shared-project-123"))
    assert c["billing enabled"].fix == "gcloud billing projects link shared-project-123 --billing-account=BBB-222"
    assert c["billing enabled"].detail == "no billing account linked"
    # No open account at all.
    c = by_name(doctor.billing_checks(ScriptRunner({
        "gcloud billing projects describe": Result(0, json.dumps({"billingEnabled": False})),
        "gcloud billing accounts list": Result(0, "[]"),
    }), "shared-project-123"))
    assert c["billing enabled"].fix.startswith("create or reopen a billing account")
    # Unreadable billing is reported as such, never as "not enabled".
    c = by_name(doctor.billing_checks(ScriptRunner({"gcloud billing projects describe": Result(1, "", "PERMISSION_DENIED: billing")}), "p"))
    assert c["billing enabled"].status == MISSING and "PERMISSION_DENIED" in c["billing enabled"].detail


def test_budget_alert_is_a_warning_only():
    number = Result(0, "123456789\n")
    budgets = [{"displayName": "other project", "budgetFilter": {"projects": ["projects/999"]}},
               {"displayName": "shared $5", "budgetFilter": {"projects": ["projects/123456789"]}}]
    c = doctor.budget_check(ScriptRunner({"gcloud billing budgets list": Result(0, json.dumps(budgets)),
                                          "gcloud projects describe": number}), "p", "AAA")
    assert c.status == OK and c.detail == "shared $5" and not c.required
    c = doctor.budget_check(ScriptRunner({"gcloud billing budgets list": Result(0, json.dumps(budgets[:1])),
                                          "gcloud projects describe": number}), "p", "AAA")
    assert c.status == WARN and "Budgets and alerts" in c.fix
    c = doctor.budget_check(ScriptRunner({"gcloud billing budgets list": Result(1, "", "API billingbudgets not enabled")}), "p", "AAA")
    assert c.status == WARN and "could not list budgets" in c.detail


def dataset(ds_id, *access):
    return Result(0, json.dumps({"datasetReference": {"datasetId": ds_id}, "access": list(access)}))


def test_dataset_isolation_in_the_shared_project():
    name = "shared project: peoplelens-* read only peoplelens_* datasets"
    listing = Result(0, json.dumps([{"datasetReference": {"datasetId": d}} for d in ("peoplelens_semantic", "otherrepo_raw")]))
    owner = {"role": "OWNER", "userByEmail": "owner@example.com"}
    reader = {"role": "READER", "userByEmail": "peoplelens-worker@p.iam.gserviceaccount.com"}
    view = {"view": {"projectId": "p", "datasetId": "peoplelens_core", "tableId": "v"}}
    clean = ScriptRunner({"bq ls": listing, "bq show --format=json p:peoplelens_semantic": dataset("peoplelens_semantic", owner, reader, view),
                          "bq show --format=json p:otherrepo_raw": dataset("otherrepo_raw", owner)})
    c = doctor.dataset_isolation_check(clean, "p", fast=False)
    assert c.status == OK and c.required and c.detail == "2 datasets checked"
    leak = ScriptRunner({"bq ls": listing, "bq show --format=json p:peoplelens_semantic": dataset("peoplelens_semantic", owner, reader),
                         "bq show --format=json p:otherrepo_raw": dataset("otherrepo_raw", owner, {"role": "READER", "iamMember": "serviceAccount:peoplelens-eval@p.iam.gserviceaccount.com"})})
    c = doctor.dataset_isolation_check(leak, "p", fast=False)
    assert c.status == MISSING and c.detail == "peoplelens-eval on otherrepo_raw"
    writer = ScriptRunner({"bq ls": listing, "bq show --format=json p:peoplelens_semantic": dataset("peoplelens_semantic", {"role": "WRITER", "userByEmail": "peoplelens-worker@p.iam.gserviceaccount.com"}),
                           "bq show --format=json p:otherrepo_raw": dataset("otherrepo_raw", owner)})
    assert doctor.dataset_isolation_check(writer, "p", fast=False).detail == "peoplelens-worker WRITER on peoplelens_semantic"
    empty = doctor.dataset_isolation_check(ScriptRunner({"bq ls": Result(0, "")}), "p", fast=False)
    assert empty.status == OK and empty.detail == "0 datasets checked"
    assert doctor.dataset_isolation_check(ScriptRunner({}), "p", fast=True).status == SKIP
    assert doctor.dataset_isolation_check(ScriptRunner({"bq ls": Result(1, "", "Access Denied")}), "p", fast=False).status == MISSING
    assert by_name(doctor.gcp_checks(ScriptRunner({**GCP_BASE, "bq ls": listing}), None, fast=True))[name].status == SKIP


def test_missing_items_are_listed_in_order(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "cloudflare_api_from_env", lambda: None)
    monkeypatch.setattr(doctor, "gcp_checks", lambda r, p, f: [checks.Check("gcp", "billing enabled", MISSING, "reopen it"),
                                                                checks.Check("gcp", "x", WARN, "y", required=False)])
    assert doctor.run(["gcp"], None, strict_governance=False, fast=True, runner=ScriptRunner({})) == 1
    out = capsys.readouterr().out
    assert "Missing (required), in order:\n1. gcp: billing enabled: reopen it" in out and "2." not in out.split("in order:")[1]


def test_bootstrap_fix_names_the_project(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "STATE_PATH", tmp_path / "state.json")
    c = doctor.deployed_checks(ScriptRunner({}), None, "shared-project-123")[0]
    assert c.status == SKIP and "--gcp-project shared-project-123 --role-emails analyst=" in c.fix
    assert "<shared-project-id>" in doctor.deployed_checks(ScriptRunner({}), None, None)[0].fix


def test_deployed_between_phases_a_and_b(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(doctor, "PROD_EVAL_ENV", tmp_path / "prod_eval.env")
    monkeypatch.setattr(doctor, "EVAL_KEY", tmp_path / "eval.json")
    assert doctor.deployed_checks(ScriptRunner({}), None, None)[0].status == SKIP
    checks.save_state({"worker_url": "https://peoplelens.lumen.workers.dev", "gcp_project": "peoplelens-abc123"})

    def http(url, headers=None):
        if url.endswith("/api/health"):
            return 503, {"missing": ["ACCESS_AUD", "ACCESS_TEAM_DOMAIN", "ROLE_BINDINGS_JSON"]}, ""
        return 302, {}, "https://lumen.cloudflareaccess.com/cdn-cgi/access/login"

    r = ScriptRunner({
        "bq show": Result(0, '{"maxTimeTravelHours": "168"}'),
        "gcloud iam service-accounts describe": Result(0, "{}"),
        "npx wrangler secret list": Result(0, '[{"name": "GCP_PROJECT_ID"}, {"name": "GCP_SA_KEY_JSON"}, {"name": "AI_GATEWAY_ID"}]'),
    })
    c = by_name(doctor.deployed_checks(r, None, None, http=http))
    assert c["/api/health"].status == MISSING and "ACCESS_AUD" in c["/api/health"].detail
    assert "--phase b" in c["/api/health"].fix
    assert c["Worker secrets"].detail == "ACCESS_TEAM_DOMAIN, ACCESS_AUD, ROLE_BINDINGS_JSON"
    assert c["unauthenticated /api/me blocked by Access"].status == OK
    assert c["dataset peoplelens_raw (time travel 48 h)"].status == MISSING  # reported 168
    assert c["dataset peoplelens_semantic (time travel 168 h)"].status == OK


def test_run_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "cloudflare_api_from_env", lambda: None)
    monkeypatch.setattr(doctor, "governance_checks", lambda r, p: [checks.Check("governance", "phase C", MISSING, "x", required=False)])
    assert doctor.run(["governance"], None, strict_governance=False, fast=True, runner=ScriptRunner({})) == 0
    assert doctor.run(["governance"], None, strict_governance=True, fast=True, runner=ScriptRunner({})) == 1
    out = capsys.readouterr().out
    assert "GROUP" in out and "STATUS" in out and "FIX" in out


IAM_POLICY = {
    "bindings": [
        {"role": "roles/bigquery.jobUser", "members": ["serviceAccount:peoplelens-worker@p.iam.gserviceaccount.com",
                                                       "serviceAccount:peoplelens-eval@p.iam.gserviceaccount.com"]},
        {"role": "roles/owner", "members": ["user:owner@example.com"]},
        {"role": "roles/bigquery.dataViewer", "members": ["serviceAccount:otherrepo-reader@p.iam.gserviceaccount.com"]},
    ]
}


def test_gcp_shared_project_isolation():
    import json

    base = dict(GCP_BASE)
    name = "shared project: peoplelens-* hold no project role but roles/bigquery.jobUser"
    ok = by_name(doctor.gcp_checks(ScriptRunner({**base, "gcloud projects get-iam-policy": Result(0, json.dumps(IAM_POLICY))}), None))
    assert ok[name].status == OK and ok[name].required  # other repos' members are not ours to judge
    leaky = {**IAM_POLICY, "bindings": IAM_POLICY["bindings"] + [
        {"role": "roles/bigquery.dataViewer", "members": ["serviceAccount:peoplelens-eval@p.iam.gserviceaccount.com"]}]}
    bad = by_name(doctor.gcp_checks(ScriptRunner({**base, "gcloud projects get-iam-policy": Result(0, json.dumps(leaky))}), None))
    assert bad[name].status == MISSING
    assert bad[name].detail == "peoplelens-eval@p.iam.gserviceaccount.com roles/bigquery.dataViewer"
    assert "remove-iam-policy-binding" in bad[name].fix
    unreadable = by_name(doctor.gcp_checks(ScriptRunner({**base, "gcloud projects get-iam-policy": Result(1, "", "PERMISSION_DENIED")}), None))
    assert unreadable[name].status == MISSING
    assert doctor.ALLOWED_PROJECT_ROLES == ["roles/bigquery.jobUser"]
    assert "<shared-project-id>" in by_name(doctor.gcp_checks(ScriptRunner({}), None))["project set"].fix


def test_deployed_reports_build_commit_and_ai_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "STATE_PATH", tmp_path / "state.json")
    env = tmp_path / "prod_eval.env"
    env.write_text("EXECUTIVE_CLIENT_ID=c.access\nEXECUTIVE_CLIENT_SECRET=s\n")
    monkeypatch.setattr(doctor, "PROD_EVAL_ENV", env)
    monkeypatch.setattr(doctor, "EVAL_KEY", tmp_path / "eval.json")
    checks.save_state({"worker_url": "https://peoplelens.lumen.workers.dev", "deployed_commit": "a" * 40})

    def http(url, headers=None):
        if url.endswith("/api/health"):
            return 200, {"ok": True, "build_commit": "b" * 40}, ""
        if headers:
            return 200, {"role": "executive", "ai_budget": {"day": "2026-10-11", "used": 120, "cap": 6000}}, ""
        return 302, {}, "https://lumen.cloudflareaccess.com/cdn-cgi/access/login"

    c = by_name(doctor.deployed_checks(ScriptRunner({"npx wrangler secret list": Result(0, "[]")}), None, None, http=http))
    assert c["/api/health build_commit is the deployed commit"].status == MISSING
    assert c["/api/me reports this repo's Workers AI share (ai_budget)"].status == OK
    assert "120 of 6000" in c["/api/me reports this repo's Workers AI share (ai_budget)"].detail


def test_audit_group(tmp_path, monkeypatch, cf_env):
    from peoplelens.ops import bootstrap_audit

    monkeypatch.setattr(doctor, "CLOUDFLARE_ENV", cf_env)
    monkeypatch.setattr(checks, "STATE_PATH", tmp_path / "state.json")
    cfg = tmp_path / "wrangler.audit.generated.jsonc"
    monkeypatch.setattr(bootstrap_audit, "GENERATED_CONFIG", cfg)
    get = "npx wrangler basin catalog get peoplelens-audit"
    active = "Catalog URI:  https://catalog.cloudflarestorage.com/a/peoplelens-audit\nWarehouse:    a_peoplelens-audit\nStatus:       active\n"
    cfg.write_text(json.dumps({"env": {"production": {"queues": {}, "r2_buckets": []}}}))
    c = by_name(doctor.audit_checks(ScriptRunner({get: Result(0, active)})))
    assert c["Basin Catalog active on peoplelens-audit"].status == OK
    assert c["generated config has no pipelines key (Workers Paid only)"].status == OK
    assert c["R2 API token for make audit-commit"].status == WARN  # optional until make audit-commit
    cfg.write_text(json.dumps({"env": {"production": {"pipelines": [{"binding": "AUDIT_STREAM"}]}}}))
    c = by_name(doctor.audit_checks(ScriptRunner({get: Result(0, "Basin Catalog is not enabled for bucket 'peoplelens-audit'.")})))
    assert c["generated config has no pipelines key (Workers Paid only)"].status == MISSING
    # Before bootstrap --with-audit, an inactive catalog and a missing config are not
    # prerequisites (the bootstrap creates both); afterwards they are required.
    cfg.unlink()
    off = "Basin Catalog is not enabled for bucket 'peoplelens-audit'."
    before = by_name(doctor.audit_checks(ScriptRunner({get: Result(0, off)}), "shared-project-123"))
    assert before["Basin Catalog active on peoplelens-audit"].status == WARN and not before["Basin Catalog active on peoplelens-audit"].required
    assert before["generated config (worker/wrangler.audit.generated.jsonc)"].status == WARN
    assert "--gcp-project shared-project-123 --phase audit" in before["Basin Catalog active on peoplelens-audit"].fix
    assert before["permission Queues: Edit (list queues)"].status == SKIP
    checks.save_state({"audit_enabled": True})
    after = by_name(doctor.audit_checks(ScriptRunner({get: Result(0, off)})))
    assert after["Basin Catalog active on peoplelens-audit"].status == MISSING and after["Basin Catalog active on peoplelens-audit"].required
    assert after["generated config (worker/wrangler.audit.generated.jsonc)"].status == MISSING


def test_audit_token_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "STATE_PATH", tmp_path / "state.json")

    class Api(FakeCf):
        def result(self, method, path, body=None):
            if path == "/r2/buckets":
                raise CloudflareError(403, "Please enable R2 through the Cloudflare Dashboard.")
            return []

    c = by_name(doctor.audit_checks(ScriptRunner({}), "p", Api()))
    assert c["permission Queues: Edit (list queues)"].status == OK
    r2 = c["permission Workers R2 Storage: Edit (list buckets)"]
    assert r2.status == MISSING and r2.required and "R2 must be activated" in r2.fix



TAG_NAMES = {"pii_high": "projects/p/locations/us/taxonomies/1/policyTags/11",
             "protected_class": "projects/p/locations/us/taxonomies/1/policyTags/12",
             "compensation": "projects/p/locations/us/taxonomies/1/policyTags/13"}


def test_governance_is_required_once_phase_c_recorded_its_tags(tmp_path, monkeypatch):
    """A rebuild without the phase C vars drops every policy tag; the doctor must fail then."""
    monkeypatch.setattr(checks, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(doctor, "cloudflare_api_from_env", lambda: None)
    assert all(not c.required for c in doctor.governance_checks(ScriptRunner({}), "p"))  # before phase C
    checks.save_state({"gcp_project": "p", "policy_tags": TAG_NAMES})
    untagged = ScriptRunner({"bq show": Result(0, json.dumps([{"name": "base_salary_eom"}]))})
    c = by_name(doctor.governance_checks(untagged, "p"))
    assert c["policy tags on the eight columns"].status == MISSING and c["policy tags on the eight columns"].required
    assert doctor.run(["governance"], "p", strict_governance=False, fast=True, runner=untagged) == 1
    schemas = {
        "p:peoplelens_raw.raw_employees": ["full_name", "work_email", "date_of_birth", "gender"],
        "p:peoplelens_raw.raw_candidates": ["full_name", "email"],
        "p:peoplelens_raw.raw_compensation_history": ["base_salary_usd"],
        "p:peoplelens_core.fct_workforce_monthly": ["base_salary_eom"],
    }
    tagged = ScriptRunner({f"bq show --schema --format=json {t}": Result(0, json.dumps([{"name": col, "policyTags": {"names": ["x"]}} for col in cols]))
                           for t, cols in schemas.items()})
    assert by_name(doctor.governance_checks(tagged, "p"))["policy tags on the eight columns"].status == OK
