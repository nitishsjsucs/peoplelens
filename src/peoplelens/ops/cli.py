"""`pl-ops`: doctor, bootstrap (phases A, B, C), deploy, smoke, audit commit, rotate-key."""

from __future__ import annotations

import argparse
import json
import os
import sys

from peoplelens.ops.checks import EVAL_KEY, WORKER_KEY, Plan, Runner, load_state, read_env_file


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
    if args.with_audit and "audit" not in groups:
        groups.append("audit")
    return run(groups, args.gcp_project, args.strict_governance, args.fast)


def _bootstrap(args) -> int:
    from peoplelens.ops import bootstrap_access, bootstrap_cloudflare, bootstrap_gcp, bootstrap_governance
    from peoplelens.ops.roles import RoleEmailError, parse_role_emails

    state = load_state()
    project = args.gcp_project or state.get("gcp_project")
    if not project:
        print("pass --gcp-project <shared-project-id> (SPEC.md 17.5)", file=sys.stderr)
        return 2
    # `--phase audit` sets up only the optional audit trail; rerunning phase A for it would
    # rebuild the warehouse (which keeps the policy tags, but is not needed).
    if args.phase == "audit":
        args.with_audit = True
        phases: list[str] = []
    else:
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
    # A real run checks the Cloudflare token before touching Google Cloud, so a missing
    # token stops it at once instead of after the warehouse steps (a dry run still
    # prints the Google Cloud plan first).
    api = _api() if not args.dry_run and ("a" in phases or "b" in phases or args.with_audit) else None
    try:
        if "a" in phases:
            print("Phase A: warehouse, gateway, first deploy (fails closed until phase B)")
            bootstrap_gcp.phase_a_gcp(project, runner, plan)
            bootstrap_cloudflare.phase_a_cloudflare(api or _api(), project, runner, plan, args.workers_subdomain)
        if "b" in phases:
            print("Phase B: Access and identities")
            bootstrap_access.phase_b(api or _api(), emails or {}, runner, plan, args.access_aud)
        if args.with_audit:
            from peoplelens.ops import bootstrap_audit

            print("Audit trail: Queue, R2 staging bucket, Basin Catalog (no Basin Pipelines: Workers Paid only)")
            bootstrap_audit.phase_audit(api or _api(), runner, plan)
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


def _deploy(args, runner: Runner | None = None) -> int:
    from peoplelens.ops.bootstrap_cloudflare import BootstrapError, deploy
    from peoplelens.ops.bootstrap_gcp import sa_email
    from peoplelens.ops.keys import KeyRotationError, delete_other_keys, key_id_of

    runner = runner or Runner(dry_run=args.dry_run)
    plan = Plan()
    secrets = None
    new_key = project = None
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
            new_key = key_id_of(WORKER_KEY)
            secrets = {"GCP_SA_KEY_JSON": json.dumps(json.loads(WORKER_KEY.read_text()))}
    try:
        deploy(runner, plan, secrets)
    except BootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if WORKER_KEY.exists():
            os.remove(WORKER_KEY)
    if args.rotate_key and new_key and project:
        # The Worker now runs on the new key: the old ones would stay valid until deleted.
        if getattr(args, "keep_old_keys", False):
            print(f"new key {new_key[:8]} deployed; old keys kept (--keep-old-keys)")
            return 0
        try:
            deleted = delete_other_keys(runner, sa_email("peoplelens-worker", project), project, new_key)
        except KeyRotationError as exc:
            print(f"new key deployed, but: {exc}", file=sys.stderr)
            return 1
        print(f"new key {new_key[:8]} deployed; deleted {len(deleted)} old key(s) of peoplelens-worker")
    return 0


def _rotate_eval_key(args, runner: Runner | None = None) -> int:
    """A fresh key for peoplelens-eval (.secrets/peoplelens-eval.json); then every other
    user-managed key of that account is deleted."""
    from peoplelens.ops.bootstrap_gcp import sa_email
    from peoplelens.ops.keys import KeyRotationError, delete_other_keys, key_id_of

    runner = runner or Runner(dry_run=args.dry_run)
    project = load_state().get("gcp_project")
    if not project:
        print("no project in .secrets/bootstrap-state.json; run make bootstrap", file=sys.stderr)
        return 2
    sa = sa_email("peoplelens-eval", project)
    fresh = EVAL_KEY.with_name("peoplelens-eval.new.json")
    res = runner.run(["gcloud", "iam", "service-accounts", "keys", "create", str(fresh), f"--iam-account={sa}",
                      f"--project={project}"], mutates=True)
    if not res.ok:
        print(f"key creation failed: {res.stderr[:300]}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("dry run: would replace .secrets/peoplelens-eval.json and delete the other user-managed keys of peoplelens-eval")
        return 0
    os.chmod(fresh, 0o600)
    new_key = key_id_of(fresh)
    os.replace(fresh, EVAL_KEY)
    if not new_key:
        print("the new key file has no private_key_id; old keys were not deleted", file=sys.stderr)
        return 1
    if args.keep_old_keys:
        print(f"new eval key {new_key[:8]} saved; old keys kept (--keep-old-keys)")
        return 0
    try:
        deleted = delete_other_keys(runner, sa, project, new_key)
    except KeyRotationError as exc:
        print(f"new eval key saved, but: {exc}", file=sys.stderr)
        return 1
    print(f"new eval key {new_key[:8]} saved to {EVAL_KEY}; deleted {len(deleted)} old key(s) of peoplelens-eval")
    return 0


AUDIT_R2_KEYS = ("CLOUDFLARE_ACCOUNT_ID", "AUDIT_R2_TOKEN", "AUDIT_R2_ACCESS_KEY_ID", "AUDIT_R2_SECRET_ACCESS_KEY")


def audit_target(catalog_uri: str | None = None, warehouse: str | None = None):
    """(R2 staging store, Basin Catalog RestCatalog) for the production audit committer,
    or a message saying what is missing."""
    from peoplelens.ops.bootstrap_audit import BUCKET
    from peoplelens.ops.checks import CLOUDFLARE_ENV
    from peoplelens.warehouse.audit_commit import r2_store

    env = read_env_file(CLOUDFLARE_ENV)
    missing = [k for k in AUDIT_R2_KEYS if not env.get(k)]
    if missing:
        return f"missing in .secrets/cloudflare.env: {', '.join(missing)} (an R2 API token with Admin Read & Write; SPEC.md 17.3)"
    state = load_state()
    uri = catalog_uri or state.get("audit_catalog_uri")
    wh = warehouse or state.get("audit_warehouse")
    if not uri or not wh:
        return ('no Catalog URI and Warehouse: run `make bootstrap ARGS="--gcp-project <shared-project-id> --phase audit"`, or pass --catalog-uri and --warehouse '
                "(R2 > peoplelens-audit > Settings > Basin Catalog)")
    from pyiceberg.catalog.rest import RestCatalog

    catalog = RestCatalog("peoplelens_audit", uri=uri, warehouse=wh, token=env["AUDIT_R2_TOKEN"])
    store = r2_store(env["CLOUDFLARE_ACCOUNT_ID"], env["AUDIT_R2_ACCESS_KEY_ID"], env["AUDIT_R2_SECRET_ACCESS_KEY"], BUCKET)
    return store, catalog


def _audit_commit(args) -> int:
    """Production committer: R2 staging (S3 API) into Basin Catalog (Iceberg REST) with pyiceberg."""
    from peoplelens.warehouse import audit_commit

    target = audit_target(args.catalog_uri, args.warehouse)
    if isinstance(target, str):
        print(target, file=sys.stderr)
        return 2
    store, catalog = target
    result = audit_commit.commit(store, catalog)
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


def _smoke(args) -> int:
    from peoplelens.ops.smoke import run

    return run(parity=not args.no_parity, drills=args.drills, with_audit=args.with_audit)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pl-ops", description="PeopleLens production operations")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="check every prerequisite and print what is missing")
    d.add_argument("--groups", default="", help="comma list: local,cloudflare,gcp,deployed,governance,audit")
    d.add_argument("--gcp-project", default=None)
    d.add_argument("--strict-governance", action="store_true")
    d.add_argument("--fast", action="store_true", help="skip the catalog freshness compile (about 25 s)")
    d.add_argument("--with-audit", action="store_true", help="also check the optional audit trail (Basin Catalog, generated config, R2 keys)")
    d.set_defaults(func=_doctor)
    b = sub.add_parser("bootstrap", help="create every cloud resource (phases A, B, C; idempotent, resumable)")
    b.add_argument("--phase", choices=["a", "b", "c", "audit"], help="one phase; `audit` is the optional audit trail alone")
    b.add_argument("--gcp-project", default=None)
    b.add_argument("--role-emails", default="")
    b.add_argument("--workers-subdomain", default=None)
    b.add_argument("--access-aud", default=None, help="dashboard fallback: the Access application AUD tag")
    b.add_argument("--with-audit", action="store_true")
    b.add_argument("--dry-run", action="store_true", help="print the plan; read-only calls still run")
    b.set_defaults(func=_bootstrap)
    dep = sub.add_parser("deploy", help="wrangler deploy --env production (secrets persist)")
    dep.add_argument("--rotate-key", action="store_true", help="upload a fresh GCP_SA_KEY_JSON, then delete the old keys")
    dep.add_argument("--keep-old-keys", action="store_true", help="with --rotate-key: do not delete the previous keys")
    dep.add_argument("--dry-run", action="store_true")
    dep.set_defaults(func=_deploy)
    s = sub.add_parser("smoke", help="production smoke checks (needs phases A and B)")
    s.add_argument("--no-parity", action="store_true")
    s.add_argument("--drills", action="store_true", help="also run the masking demo and the time-travel recovery drill")
    s.add_argument("--with-audit", action="store_true", help="step 8: commit the staged audit events and check each step 4 request is in the table once")
    s.set_defaults(func=_smoke)
    audit = sub.add_parser("audit", help="optional production audit trail")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    ac = audit_sub.add_parser("commit", help="append staged audit events from R2 to the Basin Catalog Iceberg table (dedupes on request_id)")
    ac.add_argument("--catalog-uri", default=None, help="Basin Catalog URI (default: recorded by bootstrap --with-audit)")
    ac.add_argument("--warehouse", default=None, help="Basin Catalog warehouse name (default: recorded by bootstrap --with-audit)")
    ac.set_defaults(func=_audit_commit)
    rk = sub.add_parser("rotate-key", help="deploy --rotate-key (the Worker's key), or --eval for the eval key")
    rk.add_argument("--eval", action="store_true", help="rotate .secrets/peoplelens-eval.json instead of the Worker's key")
    rk.add_argument("--keep-old-keys", action="store_true", help="do not delete the previous keys")
    rk.add_argument("--dry-run", action="store_true")
    rk.set_defaults(func=lambda a: _rotate_eval_key(a) if a.eval else _deploy(
        argparse.Namespace(rotate_key=True, dry_run=a.dry_run, keep_old_keys=a.keep_old_keys)))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
