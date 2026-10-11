"""Masking demo and time-travel recovery drill (smoke step 6), with mocked gcloud and BigQuery."""

from __future__ import annotations

import re

import pytest

from peoplelens.ops import drills
from peoplelens.ops.checks import Result, Runner


class TokenRunner(Runner):
    def run(self, cmd, **kw):
        self.log.append(cmd)
        if cmd[:3] == ["gcloud", "auth", "print-access-token"]:
            imp = next((c for c in cmd if c.startswith("--impersonate-service-account=")), None)
            return Result(0, "analyst-token" if imp else "owner-token")
        return Result(0, "")


def masking_http(analyst_values, owner_values):
    def http(url, body, token):
        assert url.endswith("/projects/peoplelens-abc123/queries")
        assert "peoplelens_core.fct_workforce_monthly" in body["query"]
        vals = analyst_values if token == "analyst-token" else owner_values
        return {"rows": [{"f": [{"v": v}]} for v in vals]}
    return http


def test_masking_demo_passes_only_when_the_analyst_sees_masked_values():
    r = TokenRunner()
    ok = drills.masking_demo("peoplelens-abc123", r, masking_http(["0", "0"], ["152300", "98100"]))
    assert ok == {"ok": True, "analyst_sees_masked_values": True, "owner_sees_real_values": True, "rows_checked": 2}
    assert any("--impersonate-service-account=peoplelens-analyst@peoplelens-abc123.iam.gserviceaccount.com" in c for c in r.log[0])
    leaked = drills.masking_demo("peoplelens-abc123", TokenRunner(), masking_http(["152300"], ["152300"]))
    assert leaked["ok"] is False and leaked["analyst_sees_masked_values"] is False


class FakeBigQuery:
    def __init__(self, restore_works=True):
        self.tables: dict[str, int] = {}
        self.statements: list[str] = []
        self.restore_works = restore_works

    def __call__(self, url, body, token):
        sql = body["query"]
        self.statements.append(sql)
        name = lambda s: re.findall(r"`[^`]+`\.peoplelens_core\.(\w+)", s)  # noqa: E731
        if sql.startswith("CREATE TABLE") and "FOR SYSTEM_TIME AS OF" in sql:
            target, source = name(sql)[:2]
            self.tables[target] = 36 if self.restore_works else 0
        elif sql.startswith("CREATE TABLE"):
            self.tables[name(sql)[0]] = 36
        elif sql.startswith("DELETE"):
            self.tables[name(sql)[0]] = 0
        elif sql.startswith("SELECT COUNT"):
            return {"rows": [{"f": [{"v": str(self.tables[name(sql)[0]])}]}]}
        elif sql.startswith("DROP TABLE"):
            self.tables.pop(name(sql)[0], None)
        return {}


def test_time_travel_drill_touches_only_scratch_tables_and_cleans_up():
    bq = FakeBigQuery()
    out = drills.time_travel_drill("peoplelens-abc123", TokenRunner(), bq, sleep=lambda s: None, now_ms=lambda: 1_791_000_000_000)
    assert out["ok"] is True and out["before"] == 36 and out["after_delete"] == 0 and out["recovered"] == 36
    mutating = [s for s in bq.statements if s.split()[0] in ("CREATE", "DELETE", "DROP")]
    assert all("drill_dim_month_" in s for s in mutating)
    assert any("FOR SYSTEM_TIME AS OF TIMESTAMP_MILLIS(1791000000000)" in s for s in bq.statements)
    assert bq.tables == {}  # both scratch tables dropped


def test_drill_reports_failure_and_still_cleans_up():
    bq = FakeBigQuery(restore_works=False)
    out = drills.time_travel_drill("peoplelens-abc123", TokenRunner(), bq, sleep=lambda s: None)
    assert out["ok"] is False and bq.tables == {}


def test_missing_token_is_a_clear_error():
    class NoAuth(Runner):
        def run(self, cmd, **kw):
            return Result(1, "", "You do not currently have an active account selected.")

    with pytest.raises(drills.DrillError, match="access token"):
        drills.masking_demo("peoplelens-abc123", NoAuth(), masking_http([], []))
