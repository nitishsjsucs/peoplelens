"""Service-account key rotation deletes the old keys; the doctor flags extra or old keys."""

from __future__ import annotations

import argparse
import json
import stat
from datetime import datetime, timezone

import pytest

from peoplelens.ops import bootstrap_cloudflare, checks, cli, doctor
from peoplelens.ops.checks import Result, Runner
from peoplelens.ops.keys import KeyRotationError, delete_other_keys, key_problems

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


class KeyRunner(Runner):
    """gcloud as far as keys go: `keys create <path>` writes a key file, `keys list` lists
    the old key and every created one, `keys delete` removes one."""

    def __init__(self, existing=("oldkey1",)):
        super().__init__()
        self.keys = {k: "2026-01-01T00:00:00Z" for k in existing}
        self.deleted: list[str] = []
        self.created = 0

    def run(self, cmd, *, cwd=None, env=None, input_text=None, mutates=False):
        self.log.append(cmd)
        if cmd[:5] == ["gcloud", "iam", "service-accounts", "keys", "create"]:
            self.created += 1
            kid = f"newkey{self.created}"
            self.keys[kid] = "2026-10-11T00:00:00Z"
            with open(cmd[5], "w") as f:
                json.dump({"type": "service_account", "private_key_id": kid}, f)
            return Result(0, "", "")
        if cmd[:5] == ["gcloud", "iam", "service-accounts", "keys", "list"]:
            sa = next(a for a in cmd if a.startswith("--iam-account=")).split("=", 1)[1]
            return Result(0, json.dumps([{"name": f"projects/p/serviceAccounts/{sa}/keys/{k}", "validAfterTime": t}
                                         for k, t in self.keys.items()]), "")
        if cmd[:5] == ["gcloud", "iam", "service-accounts", "keys", "delete"]:
            self.keys.pop(cmd[5])
            self.deleted.append(cmd[5])
            return Result(0, "", "")
        return Result(0, "", "")


@pytest.fixture()
def state(tmp_path, monkeypatch):
    d = tmp_path / ".secrets"
    d.mkdir()
    monkeypatch.setattr(checks, "STATE_PATH", d / "state.json")
    monkeypatch.setattr(cli, "WORKER_KEY", d / "worker-key.json")
    monkeypatch.setattr(cli, "EVAL_KEY", d / "peoplelens-eval.json")
    checks.save_state({"gcp_project": "p"})
    deployed = []
    monkeypatch.setattr(bootstrap_cloudflare, "deploy", lambda runner, plan, secrets=None: deployed.append(secrets))
    return d, deployed


def test_rotating_the_worker_key_deploys_it_then_deletes_the_old_one(state):
    d, deployed = state
    runner = KeyRunner()
    assert cli._deploy(argparse.Namespace(rotate_key=True, dry_run=False, keep_old_keys=False), runner=runner) == 0
    assert json.loads(deployed[0]["GCP_SA_KEY_JSON"])["private_key_id"] == "newkey1"
    assert runner.deleted == ["oldkey1"] and list(runner.keys) == ["newkey1"]
    assert not (d / "worker-key.json").exists()  # the key file never stays on disk
    kept = KeyRunner()
    assert cli._deploy(argparse.Namespace(rotate_key=True, dry_run=False, keep_old_keys=True), runner=kept) == 0
    assert kept.deleted == []


def test_rotating_the_eval_key_replaces_the_file_and_deletes_the_old_key(state):
    d, _ = state
    (d / "peoplelens-eval.json").write_text(json.dumps({"private_key_id": "oldkey1"}))
    runner = KeyRunner()
    assert cli._rotate_eval_key(argparse.Namespace(dry_run=False, keep_old_keys=False), runner=runner) == 0
    key = d / "peoplelens-eval.json"
    assert json.loads(key.read_text())["private_key_id"] == "newkey1"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert runner.deleted == ["oldkey1"]
    assert not (d / "peoplelens-eval.new.json").exists()


def test_old_keys_are_never_deleted_unless_the_new_key_is_listed():
    runner = KeyRunner()
    with pytest.raises(KeyRotationError, match="not among the keys"):
        delete_other_keys(runner, "peoplelens-worker@p.iam.gserviceaccount.com", "p", "missing-key")
    assert runner.deleted == []


def test_doctor_warns_about_a_second_key_or_an_old_one():
    assert key_problems([{"id": "a", "valid_after": "2026-10-01T00:00:00Z"}], NOW) == []
    assert key_problems([{"id": "a", "valid_after": "2026-10-01T00:00:00Z"}, {"id": "b", "valid_after": "2026-10-02T00:00:00Z"}], NOW) == [
        "2 user-managed keys (the old one is still valid)"]
    assert key_problems([{"id": "abcdefghij", "valid_after": "2026-01-01T00:00:00Z"}], NOW) == ["key abcdefgh is 283 days old"]
    c = {x.name: x for x in doctor.key_checks(KeyRunner(existing=("k1", "k2")), "p")}
    worker = c["peoplelens-worker keys (one, under 90 days old)"]
    assert worker.status == "WARN" and not worker.required and "--rotate-key" in worker.fix
    assert "rotate-key --eval" in c["peoplelens-eval keys (one, under 90 days old)"].fix
