"""`pl-ops`: doctor, bootstrap (phases A, B, C), deploy, smoke, rotate-key."""

from __future__ import annotations

import argparse
import json
import os
import sys

from peoplelens.ops.checks import WORKER_KEY, Plan, Runner, load_state, read_env_file


def _api():
    from peoplelens.ops.checks import CLOUDFLARE_ENV
    from peoplelens.ops.cloudflare_api import CloudflareApi

    env = read_env_file(CLOUDFLARE_ENV)
    if not env.get("CLOUDFLARE_ACCOUNT_ID") or not env.get("CLOUDFLARE_API_TOKEN"):
        raise SystemExit("missing .secrets/cloudflare.env with CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN (SPEC.md 17.3); run `make doctor`")
    return CloudflareApi(env["CLOUDFLARE_ACCOUNT_ID"], env["CLOUDFLARE_API_TOKEN"])


def _doctor(args) -> int:
    from peoplelens.ops.doctor import run

    groups = args.groups.split(",") if args.groups else ["local", "cloudflare", "gcp", "deployed", "governance"]
    return run(groups, args.gcp_project, args.strict_governance, args.fast)


def _bootstrap(args) -> int:
    from peoplelens.ops import bootstrap_access, bootstrap_cloudflare, bootstrap_gcp, bootstrap_governance
    from peoplelens.ops.roles import RoleEmailError, parse_role_emails

    state = load_state()
    project = args.gcp_project or state.get("gcp_project")
    if not project:
        print("pass --gcp-project peoplelens-<suffix> (SPEC.md 17.5)", file=sys.stderr)
        return 2
    phases = [args.phase] if args.phase else ["a", "b", "c"]
    emails = None
    if "b" in phases:
        if not args.role_emails:
            print("phase B needs --role-emails analyst=<email>,hrbp=<email>:Engineering+Sales,recruiter=<email>,exec=<email>", file=sys.stderr)
            return 2
        try:
            emails = parse_role_emails(args.role_emails)
        except RoleEmailError as exc:
            print(f"--role-emails: {exc}", file=sys.stderr)
            return 2
    runner = Runner(dry_run=args.dry_run)
    plan = Plan()
    try:
        if "a" in phases:
            print("Phase A: warehouse, gateway, first deploy (fails closed until phase B)")
            bootstrap_gcp.phase_a_gcp(project, runner, plan)
            bootstrap_cloudflare.phase_a_cloudflare(_api(), project, runner, plan, args.workers_subdomain)
        if "b" in phases:
            print("Phase B: Access and identities")
            bootstrap_access.phase_b(_api(), emails or {}, runner, plan, args.access_aud)
        if args.with_audit:
            from peoplelens.ops import bootstrap_audit

            print("Audit trail: Queue, R2, Basin Catalog, Basin Pipelines stream, sink and pipeline")
            bootstrap_audit.phase_audit(_api(), runner, plan)
        if "c" in phases:
            print("Phase C: governance (policy tags, masking)")
            try:
                bootstrap_governance.phase_c(project, runner, plan)
            except bootstrap_governance.BootstrapError as exc:
                print(f"phase C failed (not on the critical path): {exc}", file=sys.stderr)
                return 1
    except (bootstrap_gcp.BootstrapError, bootstrap_cloudflare.BootstrapError) as exc:
        print(f"bootstrap stopped: {exc}", file=sys.stderr)
        return 1
    print("done" + (" (dry run: nothing was changed)" if args.dry_run else "; run `make doctor`, then `make smoke`"))
    return 0


def _deploy(args) -> int:
    from peoplelens.ops.bootstrap_cloudflare import BootstrapError, deploy
    from peoplelens.ops.bootstrap_gcp import sa_email

    runner = Runner(dry_run=args.dry_run)
    plan = Plan()
    secrets = None
    if args.rotate_key:
        state = load_state()
        project = state.get("gcp_project")
        if not project:
            print("no project in .secrets/bootstrap-state.json; run make bootstrap", file=sys.stderr)
            return 2
        res = runner.run(["gcloud", "iam", "service-accounts", "keys", "create", str(WORKER_KEY),
                          f"--iam-account={sa_email('peoplelens-worker', project)}", f"--project={project}"], mutates=True)
        if not res.ok:
            print(f"key creation failed: {res.stderr[:300]}", file=sys.stderr)
            return 1
        if WORKER_KEY.exists():
            secrets = {"GCP_SA_KEY_JSON": json.dumps(json.loads(WORKER_KEY.read_text()))}
    try:
        deploy(runner, plan, secrets)
    except BootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if WORKER_KEY.exists():
            os.remove(WORKER_KEY)
    if args.rotate_key:
        print("new key deployed; delete the old key: gcloud iam service-accounts keys list --iam-account=... then keys delete <old id>")
    return 0


def _smoke(args) -> int:
    from peoplelens.ops.smoke import run

    return run(parity=not args.no_parity, drills=args.drills)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pl-ops", description="PeopleLens production operations")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="check every prerequisite and print what is missing")
    d.add_argument("--groups", default="", help="comma list: local,cloudflare,gcp,deployed,governance")
    d.add_argument("--gcp-project", default=None)
    d.add_argument("--strict-governance", action="store_true")
    d.add_argument("--fast", action="store_true", help="skip the catalog freshness compile (about 25 s)")
    d.set_defaults(func=_doctor)
    b = sub.add_parser("bootstrap", help="create every cloud resource (phases A, B, C; idempotent, resumable)")
    b.add_argument("--phase", choices=["a", "b", "c"])
    b.add_argument("--gcp-project", default=None)
    b.add_argument("--role-emails", default="")
    b.add_argument("--workers-subdomain", default=None)
    b.add_argument("--access-aud", default=None, help="dashboard fallback: the Access application AUD tag")
    b.add_argument("--with-audit", action="store_true")
    b.add_argument("--dry-run", action="store_true", help="print the plan; read-only calls still run")
    b.set_defaults(func=_bootstrap)
    dep = sub.add_parser("deploy", help="wrangler deploy --env production (secrets persist)")
    dep.add_argument("--rotate-key", action="store_true", help="upload a fresh GCP_SA_KEY_JSON")
    dep.add_argument("--dry-run", action="store_true")
    dep.set_defaults(func=_deploy)
    s = sub.add_parser("smoke", help="production smoke checks (needs phases A and B)")
    s.add_argument("--no-parity", action="store_true")
    s.add_argument("--drills", action="store_true", help="also run the masking demo and the time-travel recovery drill")
    s.set_defaults(func=_smoke)
    rk = sub.add_parser("rotate-key", help="alias for deploy --rotate-key")
    rk.add_argument("--dry-run", action="store_true")
    rk.set_defaults(func=lambda a: _deploy(argparse.Namespace(rotate_key=True, dry_run=a.dry_run)))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
