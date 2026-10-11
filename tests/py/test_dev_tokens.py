"""Local identity: RS256 keys, Access-shaped tokens, .dev.vars quoting."""

from __future__ import annotations

import json

import jwt
import pytest

from peoplelens.dev import keys, orchestrator, tokens


@pytest.fixture()
def tmp_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "KEY_DIR", tmp_path / "keys")
    real = keys.ensure_keys
    monkeypatch.setattr(tokens, "ensure_keys", lambda: real(tmp_path / "keys"))
    return tmp_path / "keys"


def test_keys_are_created_once_with_private_modes(tmp_keys):
    path, jwks = keys.ensure_keys(tmp_keys)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(tmp_keys.stat().st_mode & 0o777) == "0o700"
    again, jwks2 = keys.ensure_keys(tmp_keys)
    assert again == path and jwks2 == jwks
    assert jwks["keys"][0]["kid"] == keys.KID and jwks["keys"][0]["alg"] == "RS256"


def test_minted_token_matches_the_worker_contract(tmp_keys):
    token = tokens.mint(role="hr_business_partner")
    _, jwks = keys.ensure_keys(tmp_keys)
    public = jwt.PyJWK(jwks["keys"][0]).key
    claims = jwt.decode(token, public, algorithms=["RS256"], audience=keys.ACCESS_AUD, issuer=keys.ACCESS_ISSUER)
    assert claims["email"] == "hrbp@example.test"
    assert claims["type"] == "app" and claims["sub"]
    assert jwt.get_unverified_header(token)["kid"] == keys.KID
    assert claims["exp"] - claims["iat"] == 3600


def test_every_role_has_a_local_identity():
    for role in ("people_analytics", "hr_business_partner", "talent_acquisition", "executive"):
        assert tokens.email_for_role(role).endswith("@example.test")
    with pytest.raises(KeyError):
        tokens.email_for_role("admin")


def test_dev_vars_are_single_quoted_literals(tmp_path, monkeypatch):
    target = tmp_path / ".dev.vars"
    monkeypatch.setattr(orchestrator, "DEV_VARS", target)
    orchestrator.write_dev_vars({"ACCESS_JWKS_JSON": json.dumps({"keys": [{"n": "abc"}]}), "X": "1"})
    text = target.read_text()
    assert "ACCESS_JWKS_JSON='{\"keys\": [{\"n\": \"abc\"}]}'" in text
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ValueError):
        orchestrator.write_dev_vars({"BAD": "it's"})
