"""Parse `--role-emails analyst=<email>,hrbp=<email>:Engineering+Sales,recruiter=<email>,exec=<email>`."""

from __future__ import annotations

from peoplelens.policy import load

ALIASES = {
    "analyst": "people_analytics",
    "people_analytics": "people_analytics",
    "hrbp": "hr_business_partner",
    "hr_business_partner": "hr_business_partner",
    "recruiter": "talent_acquisition",
    "talent_acquisition": "talent_acquisition",
    "exec": "executive",
    "executive": "executive",
}
DEFAULT_HRBP_SCOPE = ["Engineering", "Sales"]


class RoleEmailError(ValueError):
    pass


def parse_role_emails(text: str) -> dict[str, dict]:
    """{role: {"email": str, "scope": {"departments": list | "all"}}} for all four roles."""
    policy = load()
    departments = policy.dimensions["department"]["values"]
    out: dict[str, dict] = {}
    for part in [p.strip() for p in text.split(",") if p.strip()]:
        if "=" not in part:
            raise RoleEmailError(f"expected role=email, got {part!r}")
        key, value = part.split("=", 1)
        role = ALIASES.get(key.strip().lower())
        if role is None:
            raise RoleEmailError(f"unknown role {key!r}")
        email, _, scope_text = value.partition(":")
        email = email.strip().lower()
        if "@" not in email or " " in email:
            raise RoleEmailError(f"invalid email for {role}")
        if role == "hr_business_partner":
            scope = [s.strip() for s in scope_text.split("+") if s.strip()] or list(DEFAULT_HRBP_SCOPE)
            bad = [s for s in scope if s not in departments]
            if bad:
                raise RoleEmailError(f"unknown departments in hrbp scope: {bad}")
            out[role] = {"email": email, "scope": {"departments": sorted(scope)}}
        else:
            if scope_text:
                raise RoleEmailError(f"only hrbp takes a department scope ({role})")
            out[role] = {"email": email, "scope": {"departments": "all"}}
    missing = [r for r in policy.roles if r not in out]
    if missing:
        raise RoleEmailError(f"missing roles: {', '.join(missing)}")
    return out


def role_bindings(emails: dict[str, dict], service_tokens: dict[str, str]) -> dict[str, dict]:
    """ROLE_BINDINGS_JSON: users as email:<email>, service tokens as svc:<client id>."""
    out: dict[str, dict] = {}
    for role, info in emails.items():
        out[f"email:{info['email']}"] = {"role": role, "scope": info["scope"]}
    for role, client_id in service_tokens.items():
        out[f"svc:{client_id}"] = {"role": role, "scope": emails[role]["scope"]}
    return out
