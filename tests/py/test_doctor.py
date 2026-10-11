"""Doctor: every group reports what is missing with the exact fix."""

from __future__ import annotations

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
    def __init__(self, status="active", fail=(), org=True, otp=True, sub="lumen"):
        self.status, self.fail, self.org, self.otp, self.sub = status, set(fail), org, otp, sub

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
    assert c["Workers Paid plan"].status == WARN and not c["Workers Paid plan"].required


def test_cloudflare_without_token_file(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "CLOUDFLARE_ENV", tmp_path / "missing.env")
    r = ScriptRunner({"npx wrangler whoami": Result(1, "You are not authenticated")})
    c = by_name(doctor.cloudflare_checks(r, None))
    assert c["wrangler login (OAuth)"].status == MISSING and c["wrangler login (OAuth)"].fix == "npx wrangler login"
    assert c[".secrets/cloudflare.env"].status == MISSING


def test_gcp_group(monkeypatch):
    r = ScriptRunner({
        "gcloud config get-value account": Result(0, "me@x.example"),
        "gcloud auth application-default print-access-token": Result(1, "", "no ADC"),
        "gcloud config get-value project": Result(0, "peoplelens-abc123"),
        "gcloud billing projects describe": Result(0, "True"),
        "gcloud services list": Result(0, "bigquery.googleapis.com\niam.googleapis.com\n"),
        "gcloud resource-manager org-policies describe": Result(1, "", "no org"),
    })
    c = by_name(doctor.gcp_checks(r, None))
    assert c["active gcloud account"].status == OK
    assert c["application default credentials"].status == MISSING
    assert c["application default credentials"].fix == "gcloud auth application-default login"
    assert c["billing enabled"].status == OK
    assert c["required APIs enabled"].status == MISSING and "datacatalog" in c["required APIs enabled"].detail
    no_gcloud = by_name(doctor.gcp_checks(ScriptRunner({}, tools=set()), None))
    assert no_gcloud["gcloud CLI"].fix == "brew install --cask gcloud-cli"


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
