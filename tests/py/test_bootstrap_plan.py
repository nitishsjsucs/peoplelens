"""Bootstrap: idempotent ordering, read-only grants, secrets hygiene, Access wiring."""

from __future__ import annotations

import json
import os

import pytest

from peoplelens.ops import bootstrap_access, bootstrap_cloudflare, bootstrap_gcp, checks
from peoplelens.ops.checks import Plan, Result, Runner
from peoplelens.ops.roles import RoleEmailError, parse_role_emails, role_bindings

EMAILS = "analyst=a@x.example,hrbp=h@x.example:Engineering+Sales,recruiter=r@x.example,exec=e@x.example"
WRITE_ROLES = ("admin", "editor", "dataeditor", "dataowner", "owner", "writer")


class FakeRunner(Runner):
    def __init__(self, results: dict[str, Result] | None = None, dry_run: bool = False):
        super().__init__(dry_run=dry_run)
        self.results = results or {}
        self.envs: list[dict] = []
        self.inputs: list[str | None] = []

    def run(self, cmd, *, cwd=None, env=None, input_text=None, mutates=False):
        self.log.append(cmd)
        self.inputs.append(input_text)
        self.envs.append(dict(env or {}))
        key = " ".join(cmd[:4])
        for prefix, res in self.results.items():
            if key.startswith(prefix):
                return res
        return Result(0, "", "")


@pytest.fixture()
def secrets(tmp_path, monkeypatch):
    d = tmp_path / ".secrets"
    for mod in (checks, bootstrap_gcp, bootstrap_cloudflare, bootstrap_access):
        for name in ("STATE_PATH", "WORKER_KEY", "EVAL_KEY", "DEPLOY_SECRETS", "PROD_EVAL_ENV"):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, d / getattr(checks, name).name)
    return d


def test_role_emails_parse_and_bindings():
    emails = parse_role_emails(EMAILS)
    assert emails["hr_business_partner"]["scope"] == {"departments": ["Engineering", "Sales"]}
    assert emails["executive"]["scope"] == {"departments": "all"}
    b = role_bindings(emails, {"executive": "0123456789abcdef.access"})
    assert b["svc:0123456789abcdef.access"] == {"role": "executive", "scope": {"departments": "all"}}
    assert b["email:a@x.example"]["role"] == "people_analytics"
    for bad in ("analyst=a@x.example", "boss=b@x.example," + EMAILS, EMAILS.replace("Sales", "Nope"), "analyst=a@x.example:Sales," + EMAILS):
        with pytest.raises(RoleEmailError):
            parse_role_emails(bad)


def test_project_roles_and_grants_are_read_only():
    roles = [r for rs in bootstrap_gcp.PROJECT_ROLES.values() for r in rs]
    assert roles and all(r == "roles/bigquery.jobUser" for r in roles)
    grants = bootstrap_gcp.render_sql("02_dataset_grants.sql", "peoplelens-abc123").lower()
    assert "roles/bigquery.dataviewer" in grants
    for w in WRITE_ROLES:
        assert w not in grants.replace("dataviewer", "")
    datasets = bootstrap_gcp.render_sql("01_datasets.sql", "peoplelens-abc123")
    assert "CREATE SCHEMA IF NOT EXISTS `peoplelens-abc123`.peoplelens_semantic" in datasets
    assert "max_time_travel_hours = 48" in datasets


def test_phase_a_gcp_describes_before_creating_and_skips_existing(secrets):
    runner = FakeRunner({"gcloud iam service-accounts describe": Result(0, "{}")})
    bootstrap_gcp.phase_a_gcp("peoplelens-abc123", runner, Plan())
    cmds = [" ".join(c) for c in runner.log]
    assert not any("service-accounts create" in c for c in cmds)
    assert cmds[0].startswith("gcloud services enable")
    assert any(c.startswith("uv run pl dbt --dialect bigquery seed") for c in cmds)
    assert any("keys create" in c and "peoplelens-worker" in c for c in cmds)
    assert any("keys create" in c and "peoplelens-eval" in c for c in cmds)
    dbt_envs = [e for c, e in zip(runner.log, runner.envs) if c[:3] == ["uv", "run", "pl"]]
    assert all(e.get("GCP_PROJECT_ID") == "peoplelens-abc123" for e in dbt_envs)


def test_phase_a_gcp_creates_missing_service_accounts(secrets):
    runner = FakeRunner({"gcloud iam service-accounts describe": Result(1, "", "NOT_FOUND")})
    bootstrap_gcp.phase_a_gcp("peoplelens-abc123", runner, Plan())
    created = [c for c in runner.log if c[:4] == ["gcloud", "iam", "service-accounts", "create"]]
    assert [c[4] for c in created] == ["peoplelens-worker", "peoplelens-eval", "peoplelens-analyst"]


def test_phase_a_gcp_stops_on_failure(secrets):
    runner = FakeRunner({"gcloud services enable": Result(1, "", "billing not enabled")})
    with pytest.raises(bootstrap_gcp.BootstrapError, match="billing"):
        bootstrap_gcp.phase_a_gcp("peoplelens-abc123", runner, Plan())


def test_deploy_uses_a_private_temporary_secrets_file(secrets):
    seen = {}

    class R(FakeRunner):
        def run(self, cmd, **kw):
            path = cmd[cmd.index("--secrets-file") + 1]
            seen["mode"] = oct(os.stat(path).st_mode & 0o777)
            seen["content"] = json.loads(open(path).read())
            return super().run(cmd, **kw)

    bootstrap_cloudflare.deploy(R(), Plan(), {"GCP_PROJECT_ID": "peoplelens-abc123"})
    assert seen == {"mode": "0o600", "content": {"GCP_PROJECT_ID": "peoplelens-abc123"}}
    assert not bootstrap_cloudflare.DEPLOY_SECRETS.exists()


def test_wrangler_never_sees_the_api_token(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret-token")
    captured = {}

    def fake_run(cmd, **kw):
        captured.update(kw["env"])

        class P:
            returncode, stdout, stderr = 0, "", ""

        return P()

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    Runner().run(["npx", "wrangler", "deploy"])
    assert "CLOUDFLARE_API_TOKEN" not in captured
    Runner().run(["gcloud", "version"])
    assert captured.get("CLOUDFLARE_API_TOKEN") == "secret-token"


class FakeApi:
    def __init__(self, tokens=None, worker=True, aud="aud-123"):
        self.tokens = tokens or []
        self.created: list[str] = []
        self.rotated: list[str] = []
        self.policies: list[tuple] = []
        self._worker = worker
        self._aud = aud

    def organization(self):
        return {"auth_domain": "lumen.cloudflareaccess.com"}

    def identity_providers(self):
        return [{"type": "onetimepin"}]

    def worker(self, name):
        return {"id": "w1"} if self._worker else None

    def ensure_access_app(self, worker_id):
        return "created", {"id": "app1", "aud": self._aud}

    def access_app(self, app_id):
        return {"aud": self._aud}

    def access_apps(self):
        return []

    def service_tokens(self):
        return self.tokens

    def create_service_token(self, name):
        self.created.append(name)
        n = len(self.created)
        return {"id": f"t{n}", "client_id": f"{n:032x}.access", "client_secret": f"s{n}"}

    def rotate_service_token(self, token_id):
        self.rotated.append(token_id)
        return {"client_secret": "rotated"}

    def ensure_policy(self, app_id, name, decision, include, precedence):
        self.policies.append((name, decision, include))
        return "created", {}


def test_phase_b_wires_access_tokens_policies_and_bindings(secrets, monkeypatch):
    checks.save_state({"worker_url": "https://peoplelens.example.workers.dev", "gcp_project": "peoplelens-abc123"})
    monkeypatch.setattr(bootstrap_access, "check_protected", lambda url: "redirected to Access")
    deployed = {}
    monkeypatch.setattr(bootstrap_access, "deploy", lambda runner, plan, s: deployed.update(s))
    api = FakeApi()
    bootstrap_access.phase_b(api, parse_role_emails(EMAILS), FakeRunner(), Plan())
    assert len(api.created) == 4
    assert deployed["ACCESS_TEAM_DOMAIN"] == "lumen.cloudflareaccess.com" and deployed["ACCESS_AUD"] == "aud-123"
    bindings = json.loads(deployed["ROLE_BINDINGS_JSON"])
    assert sum(k.startswith("svc:") for k in bindings) == 4 and sum(k.startswith("email:") for k in bindings) == 4
    assert {p[1] for p in api.policies} == {"allow", "non_identity"}
    env_file = bootstrap_access.PROD_EVAL_ENV
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    assert "EXECUTIVE_CLIENT_SECRET=" in env_file.read_text()
    state = checks.load_state()
    assert state["access_aud"] == "aud-123"


def test_tokens_without_saved_secrets_are_rotated_not_reused(secrets):
    existing = [{"id": "x1", "name": bootstrap_access.token_name(r), "client_id": f"{i:032x}.access"}
                for i, r in enumerate(bootstrap_access.ROLE_KEYS, start=1)]
    api = FakeApi(tokens=existing)
    out = bootstrap_access.ensure_service_tokens(api, Plan(), dry_run=False)
    assert api.rotated == ["x1"] * 4 and api.created == []
    assert all(t["client_secret"] == "rotated" for t in out.values())
    api2 = FakeApi(tokens=existing)
    bootstrap_access.ensure_service_tokens(api2, Plan(), dry_run=False)
    assert api2.rotated == [] and api2.created == []  # secrets are saved now: reused


def test_phase_b_prints_the_dashboard_fallback(secrets):
    checks.save_state({"worker_url": "https://peoplelens.example.workers.dev"})
    with pytest.raises(bootstrap_cloudflare.BootstrapError, match="Protect this Worker behind Access"):
        bootstrap_access.phase_b(FakeApi(worker=False), parse_role_emails(EMAILS), FakeRunner(), Plan())


def test_subdomain_must_exist_or_be_requested():
    class Api:
        def subdomain(self):
            return None

        def register_subdomain(self, name):
            return name

    with pytest.raises(bootstrap_cloudflare.BootstrapError, match="workers-subdomain"):
        bootstrap_cloudflare.ensure_subdomain(Api(), None, Plan(), False)
    assert bootstrap_cloudflare.ensure_subdomain(Api(), "lumen", Plan(), False) == "lumen"
