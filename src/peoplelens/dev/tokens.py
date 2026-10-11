"""Mint local Access-shaped JWTs for the four local identities (`pl token`)."""

from __future__ import annotations

import time

import jwt

from peoplelens.dev.keys import ACCESS_AUD, ACCESS_ISSUER, KID, ensure_keys
from peoplelens.paths import POLICY_DIR
from peoplelens.policy import load_role_bindings

ROLE_BINDINGS_PATH = POLICY_DIR / "role_bindings.local.json"


def email_for_role(role: str) -> str:
    for key, binding in load_role_bindings(ROLE_BINDINGS_PATH).items():
        if binding["role"] == role and key.startswith("email:"):
            return key[len("email:"):]
    raise KeyError(f"no local identity is bound to role {role!r}")


def mint(role: str | None = None, email: str | None = None, ttl_seconds: int = 3600) -> str:
    private_path, _ = ensure_keys()
    email = email or email_for_role(role or "people_analytics")
    now = int(time.time())
    claims = {
        "aud": [ACCESS_AUD],
        "iss": ACCESS_ISSUER,
        "sub": f"local-{email}",
        "email": email,
        "type": "app",
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(claims, private_path.read_bytes(), algorithm="RS256", headers={"kid": KID})
