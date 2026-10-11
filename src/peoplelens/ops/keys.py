"""Service-account keys for the Worker and the eval (SPEC.md 17.6 and risk 8).

Workload identity federation is not available from Workers, so `peoplelens-worker` and
`peoplelens-eval` use long-lived user-managed keys. Rotation creates a new key, puts it in
use (the Worker secret, or `.secrets/peoplelens-eval.json`), and then deletes every other
user-managed key of that service account, so the old key stops working. The doctor warns
when a service account has more than one user-managed key or one older than 90 days.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from peoplelens.ops.checks import Runner

KEY_MAX_AGE_DAYS = 90
ROTATED_ACCOUNTS = ("peoplelens-worker", "peoplelens-eval")


class KeyRotationError(RuntimeError):
    """A key could not be listed or deleted; the new key is already in use."""


def key_id_of(path: Path) -> str | None:
    """The key id (`private_key_id`) of a key file gcloud wrote."""
    try:
        return json.loads(path.read_text()).get("private_key_id") or None
    except (OSError, json.JSONDecodeError):
        return None


def user_keys(runner: Runner, sa: str, project: str) -> list[dict] | None:
    """[{id, valid_after}] for the user-managed keys of `sa`; None when they cannot be listed."""
    res = runner.run(["gcloud", "iam", "service-accounts", "keys", "list", f"--iam-account={sa}", "--managed-by=user",
                      f"--project={project}", "--format=json"])
    try:
        data = json.loads(res.stdout) if res.ok else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, list):
        return None
    return [{"id": str(k.get("name", "")).rsplit("/", 1)[-1], "valid_after": k.get("validAfterTime")}
            for k in data if isinstance(k, dict) and k.get("name")]


def delete_other_keys(runner: Runner, sa: str, project: str, keep_id: str) -> list[str]:
    """Delete every user-managed key of `sa` except `keep_id`; returns the deleted ids."""
    keys = user_keys(runner, sa, project)
    if keys is None:
        raise KeyRotationError(f"could not list the keys of {sa}; delete the old ones by hand: gcloud iam service-accounts keys list --iam-account={sa}")
    if not any(k["id"] == keep_id for k in keys):
        raise KeyRotationError(f"the new key {keep_id} is not among the keys of {sa}; nothing was deleted")
    deleted = []
    for k in keys:
        if k["id"] == keep_id:
            continue
        res = runner.run(["gcloud", "iam", "service-accounts", "keys", "delete", k["id"], f"--iam-account={sa}",
                          f"--project={project}", "--quiet"], mutates=True)
        if not res.ok:
            raise KeyRotationError(f"deleting key {k['id']} of {sa} failed: {(res.stderr or res.stdout).strip()[:200]}")
        deleted.append(k["id"])
    return deleted


def key_problems(keys: list[dict], now: datetime | None = None) -> list[str]:
    """Why a service account's keys need attention: more than one user-managed key, or a key
    older than KEY_MAX_AGE_DAYS."""
    now = now or datetime.now(timezone.utc)
    out = []
    if len(keys) > 1:
        out.append(f"{len(keys)} user-managed keys (the old one is still valid)")
    for k in keys:
        try:
            made = datetime.fromisoformat(str(k.get("valid_after")).replace("Z", "+00:00"))
        except ValueError:
            continue
        age = (now - made).days
        if age > KEY_MAX_AGE_DAYS:
            out.append(f"key {k['id'][:8]} is {age} days old")
    return out
