"""Local RS256 keys for Access JWT verification (local and CI only)."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from peoplelens.paths import DEV_DIR

KEY_DIR = DEV_DIR / "keys"
PRIVATE_PATH = KEY_DIR / "access-local.pem"
JWKS_PATH = KEY_DIR / "jwks.json"
KID = "peoplelens-local-1"
ACCESS_AUD = "peoplelens-local"
ACCESS_ISSUER = "https://peoplelens-local.cloudflareaccess.test"


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def ensure_keys(key_dir: Path = KEY_DIR) -> tuple[Path, dict]:
    """Create the key pair once; return (private key path, JWKS)."""
    private_path = key_dir / PRIVATE_PATH.name
    jwks_path = key_dir / JWKS_PATH.name
    if private_path.exists() and jwks_path.exists():
        return private_path, json.loads(jwks_path.read_text())
    key_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(key_dir, 0o700)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    private_path.write_bytes(pem)
    os.chmod(private_path, 0o600)
    pub = key.public_key().public_numbers()
    jwks = {"keys": [{"kty": "RSA", "kid": KID, "alg": "RS256", "use": "sig", "n": _b64(pub.n), "e": _b64(pub.e)}]}
    jwks_path.write_text(json.dumps(jwks))
    return private_path, jwks
