"""Productive time and "clicking around" in employee engagement (product owner, 16 Nov 2026):
active minutes followed by output within the output window (20 minutes by default) are
productive, the rest active with no output; a 20-minute block with no output and more than
N screen changes (10 by default) is clicking around. Both numbers are SuperAdmin settings."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import add_batch_pages, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import activity_settings as settings
from audit_core.db import set_tenant_context
from audit_core.dependencies import HumanAdminRequest, require_super_admin_request
from audit_core.engagement_reports import (
    build_engagement,
    day_analysis,
    person_day,
    productive_minutes,
)
from audit_core.main import app
from audit_core.security_integration import SecurityAdminContext
from audit_core.task_manager import Caller, ist_today
from audit_core.task_manager_reports import record_activity


def _states(**runs: str) -> str:
    out = ["."] * 1440
    for key, run in runs.items():
        start = int(key[1:3]) * 60 + int(key[4:6])
        out[start:start + len(run)] = list(run)
    return "".join(out)


def test_an_active_minute_is_productive_when_output_follows_within_the_window():
    active = list(range(600, 640))  # 10:00-10:39
    assert productive_minutes(active, [620], 20) == 21          # 10:00-10:20 lead to 10:20
    assert productive_minutes(active, [620], 5) == 6            # 10:15-10:20 only
    assert productive_minutes(active, [], 20) == 0
    assert productive_minutes(active, [599, 700], 20) == 0      # before, or too long after
    assert productive_minutes(active, [-1, 639], 60) == 40      # another day's output (-1) is ignored


def test_clicking_tabs_with_nothing_saved_is_clicking_around_not_work():
    # 10:00-10:59 clicking all the time; output only at 10:50.
    states = _states(t10_00="K" * 60)
    tabs = list(range(600, 640, 2))  # 20 screen changes 10:00-10:39, 10 in each of the first two blocks
    tabs += [601, 621]               # 11 in each
    a = day_analysis(states, [650], tabs, output_window=20, hop_changes=10)
    assert [b["state"] for b in a["blocks"]] == ["CLICKING_AROUND", "CLICKING_AROUND", "WORKING"]
    assert [b["screenChanges"] for b in a["blocks"]] == [11, 11, 0]
    assert a["blockCounts"] == {"WORKING": 1, "CLICKING_AROUND": 2, "ACTIVE_NO_OUTPUT": 0, "IDLE": 0, "AWAY": 0}
    # 10:30-10:50 lead to the output; the rest is active with no output.
    assert (a["activeMinutes"], a["productiveMinutes"], a["activeNoOutputMinutes"]) == (60, 21, 39)
    # A higher threshold: the same blocks are only "active, no output".
    calm = day_analysis(states, [650], tabs, output_window=20, hop_changes=15)
    assert [b["state"] for b in calm["blocks"]] == ["ACTIVE_NO_OUTPUT", "ACTIVE_NO_OUTPUT", "WORKING"]
    # A block with output is working however many screens were opened.
    busy = day_analysis(states, [605, 625, 650], tabs, output_window=20, hop_changes=10)
    assert [b["state"] for b in busy["blocks"]] == ["WORKING", "WORKING", "WORKING"]


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="productive")
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _super_admin(user: str = "sa-engagement"):
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id=user, bearer_token="t",
        admin_context=SecurityAdminContext(user_id=user, is_super_admin=True, admin_scopes=()))


def _settings(journey) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return settings.engagement_settings(connection, journey.tenant_id)


def test_superadmin_sets_the_two_numbers_per_project(journey):
    _super_admin()
    client = TestClient(app, raise_server_exceptions=False)
    before = _settings(journey)
    body = client.put("/v1/admin/activity-settings",
                      json={"tenantId": journey.tenant_id, "outputWindowMinutes": 30, "hopScreenChanges": 15})
    assert body.status_code == 200, body.text
    data = body.json()
    mine = next(p for p in data["projects"] if p["tenantId"] == journey.tenant_id)
    assert (mine["outputWindowMinutes"], mine["hopScreenChanges"], mine["minutes"]) == (30, 15, None)
    assert _settings(journey) == {"outputWindowMinutes": 30, "hopScreenChanges": 15}
    change = next(c for c in data["changes"] if c["tenantId"] == journey.tenant_id)
    assert (change["outputWindowMinutes"], change["hopScreenChanges"], change["setting"],
            change["changedByActorId"]) == (30, 15, "ENGAGEMENT", "sa-engagement")
    assert data["engagement"]["outputWindowMinutes"]["default"] == 20
    assert data["engagement"]["hopScreenChanges"]["default"] == 10
    # Only the key sent changes; the reading window is untouched.
    client.put("/v1/admin/activity-settings", json={"tenantId": journey.tenant_id, "hopScreenChanges": 12})
    assert _settings(journey) == {"outputWindowMinutes": 30, "hopScreenChanges": 12}
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert settings.reading_window(connection, journey.tenant_id)["scope"] in ("ALL", "DEFAULT")
    # Out of range: refused, nothing changes.
    for bad in ({"outputWindowMinutes": 4}, {"outputWindowMinutes": 61}, {"hopScreenChanges": 2},
                {"hopScreenChanges": 51}):
        assert client.put("/v1/admin/activity-settings",
                          json={"tenantId": journey.tenant_id, **bad}).status_code in (400, 422)
    assert _settings(journey) == {"outputWindowMinutes": 30, "hopScreenChanges": 12}
    # Removed for the Project: back to the all-Projects value or the default.
    client.put("/v1/admin/activity-settings",
               json={"tenantId": journey.tenant_id, "outputWindowMinutes": None, "hopScreenChanges": None})
    assert _settings(journey) == before
    # The all-Projects value cannot be emptied.
    assert client.put("/v1/admin/activity-settings",
                      json={"tenantId": None, "hopScreenChanges": None}).status_code in (400, 422)


def test_the_all_projects_value_applies_and_keeps_a_reading_window(journey):
    """Inside a transaction that is rolled back: other tests share the all-Projects row."""
    with journey.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("DELETE FROM auditcore.p2_activity_settings WHERE tenant_id IS NULL"))
            settings.set_engagement_settings(connection, tenant_id=None, values={"outputWindowMinutes": 45},
                                             actor_id="sa-all")
            row = connection.execute(text(
                "SELECT reading_window_minutes, output_window_minutes FROM auditcore.p2_activity_settings "
                "WHERE tenant_id IS NULL")).one()
            assert tuple(row) == (settings.DEFAULT_READING_WINDOW_MINUTES, 45)
            set_tenant_context(connection, journey.tenant_id)
            assert settings.engagement_settings(connection, journey.tenant_id) == {
                "outputWindowMinutes": 45, "hopScreenChanges": 10}
        finally:
            transaction.rollback()


def test_engagement_and_the_persons_day_split_active_time(journey):
    day = ist_today() - timedelta(days=1)
    at = lambda hhmm: datetime.fromisoformat(f"{day.isoformat()}T{hhmm}:00+05:30")
    caller = Caller(actor_id=journey.actor_id, role="PC")
    # 10:00-10:39 clicking, 20 tab changes in the first block with nothing saved; a file at 10:35.
    states = _states(t10_00="K" * 40)
    visits = [{"at": at(f"10:{m:02d}").isoformat(), "screen": f"Tab {m}"} for m in range(20)]
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        assert record_activity(c, tenant_id=journey.tenant_id, caller=caller, now=datetime.now(UTC), payload={
            "date": day.isoformat(), "activeSeconds": 2400, "idleSeconds": 0, "backgroundSeconds": 0,
            "hourlyActiveMinutes": [0] * 24, "minuteStates": states, "visits": visits,
            "firstActiveAt": at("10:00").isoformat(), "lastActiveAt": at("10:39").isoformat()})
    batch, _ = add_batch_pages(journey, [("booking_form", "READY")])
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_upload_batches SET created_at_utc=:at, uploaded_by_actor_id=:a "
                       "WHERE batch_id=:b"), {"at": at("10:35"), "a": journey.actor_id, "b": batch})
        # The journey itself was started by the same person today, not on that day.
        report = build_engagement(c, tenant_id=journey.tenant_id, start_day=day, end_day=day)
        mine = person_day(c, tenant_id=journey.tenant_id, actor_id=journey.actor_id, day=day)
    me = next(p for p in report["people"] if p["actorId"] == journey.actor_id)
    totals = me["totals"]
    # 10:15-10:35 lead to the file (21 minutes); 10:00-10:14 and 10:36-10:39 do not.
    assert (totals["detailActiveMinutes"], totals["productiveMinutes"], totals["activeNoOutputMinutes"]) == (40, 21, 19)
    assert totals["clickingAroundBlocks"] == 1 and totals["efficacy"]["productivePct"] == 52
    assert report["outputWindowMinutes"] == 20 and report["hopScreenChanges"] == 10
    assert any("clicking around" in n for n in report["notes"])
    (row,) = me["days"]
    assert row["productiveMinutes"] == 21 and row["blockCounts"]["CLICKING_AROUND"] == 1
    assert [b["state"] for b in mine["analysis"]["blocks"]] == ["CLICKING_AROUND", "WORKING"]
    assert mine["analysis"]["productiveMinutes"] == 21 and mine["hopScreenChanges"] == 10
