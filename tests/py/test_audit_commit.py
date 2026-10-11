"""The audit committer (SPEC.md 25.4 A and section 13): staged NDJSON objects into the
Iceberg table with an exactly-once effect. Runs against a real pyiceberg SqlCatalog and
a staging directory on a pyarrow LocalFileSystem, the same FileSystem interface the
production R2 store uses."""

from __future__ import annotations

import hashlib
import json

import pyarrow as pa
import pytest

from peoplelens import cli
from peoplelens.warehouse import audit_commit
from peoplelens.warehouse.audit_commit import (
    QUARANTINE_PREFIX,
    SNAPSHOT_OBJECTS,
    SNAPSHOT_SHA256,
    commit,
    local_store,
    staged_ids,
)
from peoplelens.warehouse.audit_sink import FIELDS, arrow_schema, load_or_create_table, local_catalog


def ev(rid: str, ts: str = "2026-10-10T12:00:00.000Z", **extra) -> dict:
    return {**{f: None for f in FIELDS}, "ts": ts, "request_id": rid, "role": "executive", "action": "answer", **extra}


def ndjson(*events: dict) -> bytes:
    return "".join(json.dumps(e) + "\n" for e in sorted(events, key=lambda e: e["request_id"])).encode()


def batch_key(*ids: str, day: str = "2026-10-10") -> str:
    return f"staging/v1/dt={day}/{hashlib.sha256(chr(10).join(sorted(ids)).encode()).hexdigest()[:32]}.ndjson"


def direct_key(rid: str, day: str = "2026-10-10") -> str:
    return f"staging/v1/dt={day}/direct-{rid}.ndjson"


def table_ids(catalog) -> list[str]:
    return sorted(load_or_create_table(catalog).scan().to_arrow().column("request_id").to_pylist())


@pytest.fixture
def env(tmp_path):
    return local_store(tmp_path / "staging"), local_catalog(tmp_path / "ice")


def test_each_request_id_is_appended_once_across_duplicate_objects(env):
    store, catalog = env
    store.put(batch_key("a", "b", "c"), ndjson(ev("a"), ev("b"), ev("c")))
    store.put(batch_key("c", "d"), ndjson(ev("c"), ev("d")))  # c delivered twice (a redelivered message)
    store.put(direct_key("d"), ndjson(ev("d")))  # d also put directly (send failed after the slot)
    result = commit(store, catalog)
    assert result.objects == 3 and result.events == 6 and result.duplicates_in_run == 2
    assert result.appended == 4 and result.deleted == 3 and result.quarantined == []
    assert table_ids(catalog) == ["a", "b", "c", "d"]
    assert store.list() == []
    snap = load_or_create_table(catalog).current_snapshot()
    assert snap.summary[SNAPSHOT_OBJECTS] == "3"
    assert snap.summary[SNAPSHOT_SHA256] == hashlib.sha256(
        "\n".join(sorted([batch_key("a", "b", "c"), batch_key("c", "d"), direct_key("d")])).encode()).hexdigest()


def test_one_snapshot_per_commit_and_replay_appends_nothing(env):
    store, catalog = env
    store.put(batch_key("a", "b"), ndjson(ev("a"), ev("b")))
    assert commit(store, catalog).appended == 2
    snapshots = len(load_or_create_table(catalog).snapshots())
    # The same objects staged again (a replayed batch, a re-put direct event) append 0 rows.
    store.put(batch_key("a", "b"), ndjson(ev("a"), ev("b")))
    store.put(direct_key("b"), ndjson(ev("b")))
    again = commit(store, catalog)
    assert again.appended == 0 and again.already_in_table == 2 and again.deleted == 2
    assert len(load_or_create_table(catalog).snapshots()) == snapshots
    assert table_ids(catalog) == ["a", "b"]
    assert store.list() == []


def test_a_crash_between_append_and_delete_appends_nothing_on_rerun(env, monkeypatch):
    store, catalog = env
    store.put(batch_key("x", "y"), ndjson(ev("x"), ev("y")))
    real_delete = store.delete
    monkeypatch.setattr(store, "delete", lambda key: (_ for _ in ()).throw(OSError("crash before delete")))
    with pytest.raises(OSError):
        commit(store, catalog)
    assert table_ids(catalog) == ["x", "y"]  # appended
    assert len(store.list()) == 1  # but not deleted
    monkeypatch.setattr(store, "delete", real_delete)
    rerun = commit(store, catalog)
    assert rerun.appended == 0 and rerun.already_in_table == 2 and rerun.deleted == 1
    assert table_ids(catalog) == ["x", "y"]


def test_a_conflicting_commit_is_retried_after_reloading_and_deduping(env, monkeypatch):
    from pyiceberg.exceptions import CommitFailedException

    store, catalog = env
    store.put(batch_key("p", "q", "r"), ndjson(ev("p"), ev("q"), ev("r")))
    real = load_or_create_table(catalog)

    class Racing:
        """The first append loses a race: another committer appended q meanwhile."""

        def __init__(self):
            self.calls = 0

        def __getattr__(self, name):
            return getattr(real, name)

        def append(self, df, snapshot_properties=None):
            self.calls += 1
            if self.calls == 1:
                other = load_or_create_table(catalog)
                other.append(pa.Table.from_pylist([ev("q")], schema=arrow_schema()))
                raise CommitFailedException("requirement failed: branch main has changed")
            return real.append(df, snapshot_properties=snapshot_properties or {})

    racing = Racing()
    monkeypatch.setattr(audit_commit, "load_or_create_table", lambda c: racing)
    result = commit(store, catalog, sleep=lambda s: None)
    assert result.attempts == 2 and result.appended == 2
    assert table_ids(catalog) == ["p", "q", "r"]  # q exactly once


def test_an_object_with_an_invalid_line_is_quarantined_whole(env):
    store, catalog = env
    bad = ndjson(ev("m")) + json.dumps({**ev("n"), "question": "how many people in Legal"}).encode() + b"\n"
    store.put(batch_key("m", "n"), bad)
    store.put(direct_key("o"), ndjson(ev("o")))
    store.put(direct_key("not-json"), b"{nope\n")
    result = commit(store, catalog)
    assert sorted(result.quarantined) == sorted([batch_key("m", "n"), direct_key("not-json")])
    assert result.appended == 1 and table_ids(catalog) == ["o"]  # m is never partly committed
    assert store.list() == []
    q = store.list(QUARANTINE_PREFIX)
    assert len(q) == 2 and all(k.startswith(QUARANTINE_PREFIX) for k in q)
    assert store.read(QUARANTINE_PREFIX + batch_key("m", "n")[len("staging/v1/"):]) == bad


def test_keys_outside_the_two_patterns_are_ignored(env):
    store, catalog = env
    store.put("staging/v1/dt=2026-10-10/notes.ndjson", ndjson(ev("z")))
    store.put("staging/v1/dt=2026-10-10/" + "b" * 32 + ".ndjson.tmp", ndjson(ev("z")))
    assert commit(store, catalog).appended == 0
    assert len(store.list()) == 2


def test_the_dedupe_window_spans_two_days_before_and_one_after(env):
    """The anti-join scans table rows whose ts lies in [earliest staged day - 2 days, latest staged
    day + 1 day]. A redelivered event keeps its original ts, so its table row is always inside;
    the margins are defensive. This pins both edges of the window: a table row just inside each
    edge is found (its request_id is not appended again), one just outside is not."""
    store, catalog = env
    table_rows = {
        "lo_in": "2026-10-06T00:00:00.000Z",   # earliest staged day 2026-10-08 minus 2 days
        "lo_out": "2026-10-05T23:59:59.999Z",
        "hi_in": "2026-10-11T23:59:59.999Z",   # latest staged day 2026-10-10 plus 1 day
        "hi_out": "2026-10-12T00:00:00.000Z",
    }
    store.put(batch_key(*table_rows, day="2026-10-06"), ndjson(*(ev(r, ts=t) for r, t in table_rows.items())))
    assert commit(store, catalog).appended == 4
    # The same request_ids staged again with ts on 2026-10-08 and 2026-10-10 (an artificial drift).
    staged = [ev("lo_in", ts="2026-10-08T01:00:00.000Z"), ev("lo_out", ts="2026-10-08T01:00:00.000Z"),
              ev("hi_in", ts="2026-10-10T23:00:00.000Z"), ev("hi_out", ts="2026-10-10T23:00:00.000Z")]
    store.put(batch_key(*(e["request_id"] for e in staged), day="2026-10-08"), ndjson(*staged))
    result = commit(store, catalog)
    assert result.already_in_table == 2 and result.appended == 2
    assert table_ids(catalog) == sorted(["hi_in", "hi_out", "hi_out", "lo_in", "lo_out", "lo_out"])


def test_staged_ids_counts_unique_request_ids(env):
    store, _ = env
    store.put(batch_key("a", "b"), ndjson(ev("a"), ev("b")))
    store.put(direct_key("b"), ndjson(ev("b")))
    assert staged_ids(store) == {"a", "b"}


def test_r2_store_uses_the_s3_api_endpoint(monkeypatch):
    seen = {}

    class FakeS3:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(audit_commit.pafs, "S3FileSystem", FakeS3)
    store = audit_commit.r2_store("acct123", "key-id", "secret", "peoplelens-audit")
    assert seen == {"access_key": "key-id", "secret_key": "secret", "endpoint_override": "acct123.r2.cloudflarestorage.com",
                    "scheme": "https", "region": "auto"}
    assert store.kind == "r2" and store.base == "peoplelens-audit"


def test_pl_audit_commit_checks_the_expected_row_count(tmp_path, monkeypatch, capsys):
    import peoplelens.paths as paths
    from peoplelens.warehouse import audit_sink

    monkeypatch.setattr(paths, "AUDIT_STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(audit_sink, "ICEBERG_DIR", tmp_path / "ice")
    monkeypatch.setattr(audit_sink.local_catalog, "__defaults__", (tmp_path / "ice",))
    store = local_store(tmp_path / "staging")
    store.put(batch_key("a", "b"), ndjson(ev("a"), ev("b")))
    assert cli.main(["audit", "commit", "--expect-rows", "2", "--wait-s", "0"]) == 0
    store.put(direct_key("a"), ndjson(ev("a")))
    assert cli.main(["audit", "commit", "--expect-rows", "0"]) == 0
    store.put(direct_key("c"), ndjson(ev("c")))
    assert cli.main(["audit", "commit", "--expect-rows", "5", "--wait-s", "0"]) == 1
    assert "expected 5 appended rows, got 1" in capsys.readouterr().err


def test_pl_audit_commit_replays_a_kept_copy_and_appends_nothing(tmp_path, monkeypatch, capsys):
    """What the CI e2e job runs: commit with --keep-copy, then redeliver every committed object."""
    import peoplelens.paths as paths
    from peoplelens.warehouse import audit_sink

    monkeypatch.setattr(paths, "AUDIT_STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(audit_sink, "ICEBERG_DIR", tmp_path / "ice")
    monkeypatch.setattr(audit_sink.local_catalog, "__defaults__", (tmp_path / "ice",))
    store = local_store(tmp_path / "staging")
    store.put(batch_key("a", "b", "c"), ndjson(ev("a"), ev("b"), ev("c")))
    store.put(direct_key("c"), ndjson(ev("c")))
    copy = tmp_path / "replay"
    assert cli.main(["audit", "commit", "--expect-rows", "3", "--wait-s", "0", "--keep-copy", str(copy)]) == 0
    assert store.list() == [] and len(list(copy.rglob("*.ndjson"))) == 2
    capsys.readouterr()
    assert cli.main(["audit", "commit", "--restage-from", str(copy), "--expect-rows", "0", "--expect-already", "3"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["objects"] == 2 and result["events"] == 4 and result["appended"] == 0 and result["already_in_table"] == 3
    assert table_ids(local_catalog(tmp_path / "ice")) == ["a", "b", "c"]
    # Without the restage there is nothing to replay, and --expect-already says so.
    assert cli.main(["audit", "commit", "--expect-rows", "0", "--expect-already", "3"]) == 1
    assert "expected 3 events already in the table, got 0" in capsys.readouterr().err


def test_pl_ops_audit_commit_needs_the_r2_keys_and_uses_rest_catalog_and_r2(tmp_path, monkeypatch, capsys):
    import pyiceberg.catalog.rest as rest

    from peoplelens.ops import checks
    from peoplelens.ops import cli as ops_cli

    env = tmp_path / "cloudflare.env"
    monkeypatch.setattr(checks, "CLOUDFLARE_ENV", env)
    monkeypatch.setattr(checks, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(ops_cli, "load_state", checks.load_state)
    env.write_text("CLOUDFLARE_ACCOUNT_ID=acct123\nCLOUDFLARE_API_TOKEN=t\n")
    assert ops_cli.main(["audit", "commit"]) == 2
    assert "AUDIT_R2_TOKEN" in capsys.readouterr().err
    env.write_text("CLOUDFLARE_ACCOUNT_ID=acct123\nAUDIT_R2_TOKEN=tok\nAUDIT_R2_ACCESS_KEY_ID=kid\nAUDIT_R2_SECRET_ACCESS_KEY=sec\n")
    assert ops_cli.main(["audit", "commit"]) == 2  # no Catalog URI recorded yet
    assert "Catalog URI" in capsys.readouterr().err
    checks.save_state({"audit_catalog_uri": "https://catalog.cloudflarestorage.com/acct123/peoplelens-audit",
                       "audit_warehouse": "acct123_peoplelens-audit"})
    seen = {}
    monkeypatch.setattr(rest, "RestCatalog", lambda name, **kw: seen.setdefault("catalog", (name, kw)) and "catalog")
    monkeypatch.setattr(audit_commit, "r2_store", lambda *a: seen.setdefault("store", a) and "store")
    monkeypatch.setattr(audit_commit, "commit", lambda store, catalog: seen.setdefault("commit", (store, catalog)) and audit_commit.CommitResult(appended=3))
    assert ops_cli.main(["audit", "commit"]) == 0
    assert seen["catalog"] == ("peoplelens_audit", {"uri": "https://catalog.cloudflarestorage.com/acct123/peoplelens-audit",
                                                    "warehouse": "acct123_peoplelens-audit", "token": "tok"})
    assert seen["store"] == ("acct123", "kid", "sec", "peoplelens-audit")
    assert seen["commit"] == ("store", "catalog")
    assert json.loads(capsys.readouterr().out)["appended"] == 3
