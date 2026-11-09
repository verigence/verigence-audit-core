"""Active time counts real work only (product owner 2026-11-09): a minute is active when a
click, tap or key was in it or in the reading window before it (3 minutes by default,
SuperAdmin sets 1-10, per Project or for all); a scrolled minute with no click is S (idle,
kept apart). Real Postgres."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import activity_settings as settings
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_human_principal,
    require_super_admin_request,
)
from audit_core.engagement_reports import build_engagement, day_analysis
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.security_integration import SecurityAdminContext
from audit_core.task_manager import ist_today
from audit_core.task_manager_reports import (
    MINUTE_RANK,
    clean_minute_states,
    merge_minute_states,
)


def _states(**runs: str) -> str:
    out = ["."] * 1440
    for key, run in runs.items():
        start = int(key[1:3]) * 60 + int(key[4:6])
        out[start:start + len(run)] = list(run)
    return "".join(out)


# ── minute states ────────────────────────────────────────────────────────────
def test_the_server_keeps_the_scroll_only_minute_and_ranks_it_between_idle_and_active():
    states = _states(t09_00="KAAAS" + "S" * 9 + "I")
    assert clean_minute_states(states) == states                       # S is no longer dropped
    assert clean_minute_states(states.replace("S", "Z", 1)) is None    # anything unknown still is
    assert MINUTE_RANK["I"] < MINUTE_RANK["S"] < MINUTE_RANK["A"] < MINUTE_RANK["K"]
    old = _states(t09_00="I" * 5 + "S" * 5)
    new = _states(t09_00="S" * 5 + "A" * 5)
    assert merge_minute_states(old, new) == _states(t09_00="S" * 5 + "A" * 5)
    assert merge_minute_states(new, old) == merge_minute_states(old, new)


def test_scrolling_with_no_click_is_idle_and_reported_apart():
    # 10:00 click, reading to 10:03; 10:04-10:15 scrolling only (S); 10:16-10:19 idle; 10:20 click.
    states = _states(t10_00="KAAA" + "S" * 12 + "I" * 4 + "KAAA")
    a = day_analysis(states, [600, 620])
    assert a["activeMinutes"] == 8 and a["scrollOnlyMinutes"] == 12
    assert a["scrollOnly"] == [{"from": "10:04", "to": "10:16", "minutes": 12}]
    first = a["blocks"][0]
    assert first["activeMinutes"] == 4 and first["idleMinutes"] == 16 and first["scrollOnlyMinutes"] == 12
    # A day of the earlier rule (no S at all) still finds scrolling as active minutes with no click.
    legacy = day_analysis(_states(t10_00="KAAA" + "A" * 12 + "KAAA"), [600])
    assert legacy["scrollOnly"] == [{"from": "10:01", "to": "10:16", "minutes": 15}]


# ── the reading window setting ───────────────────────────────────────────────
@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="reading")
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _super_admin(user: str = "sa-1"):
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id=user, bearer_token="t",
        admin_context=SecurityAdminContext(user_id=user, is_super_admin=True, admin_scopes=()))


def _window(journey) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return settings.reading_window(connection, journey.tenant_id)


def test_the_window_is_three_minutes_unless_superadmin_sets_it(journey):
    with journey.engine.begin() as connection:
        connection.execute(text("SELECT 1"))
        has_all = connection.execute(text(
            "SELECT reading_window_minutes FROM auditcore.p2_activity_settings WHERE tenant_id IS NULL")).scalar()
    window = _window(journey)
    assert window == ({"minutes": settings.DEFAULT_READING_WINDOW_MINUTES, "scope": "DEFAULT"} if has_all is None
                      else {"minutes": int(has_all), "scope": "ALL"})
    _super_admin("sa-reading")
    client = TestClient(app, raise_server_exceptions=False)
    body = client.put("/v1/admin/activity-settings", json={"tenantId": journey.tenant_id, "readingWindowMinutes": 5})
    assert body.status_code == 200, body.text
    data = body.json()
    assert {"tenantId": journey.tenant_id, "minutes": 5} == {k: v for k, v in next(
        p for p in data["projects"] if p["tenantId"] == journey.tenant_id).items() if k in ("tenantId", "minutes")}
    assert _window(journey) == {"minutes": 5, "scope": "PROJECT"}
    change = next(c for c in data["changes"] if c["tenantId"] == journey.tenant_id)
    assert (change["minutes"], change["previousMinutes"], change["changedByActorId"]) == (5, None, "sa-reading")
    assert change["changedAtUtc"]
    # Out of range: refused; nothing changes.
    for bad in (0, 11):
        assert client.put("/v1/admin/activity-settings",
                          json={"tenantId": journey.tenant_id, "readingWindowMinutes": bad}).status_code in (400, 422)
    assert _window(journey)["minutes"] == 5
    # The Project's own value removed: it follows the all-Projects value again; both changes are kept.
    data = client.put("/v1/admin/activity-settings",
                      json={"tenantId": journey.tenant_id, "readingWindowMinutes": None}).json()
    assert _window(journey)["scope"] in ("ALL", "DEFAULT")
    mine = [c for c in data["changes"] if c["tenantId"] == journey.tenant_id]
    assert [(c["previousMinutes"], c["minutes"]) for c in mine] == [(5, None), (None, 5)]
    # The all-Projects value cannot be emptied.
    assert client.put("/v1/admin/activity-settings", json={"tenantId": None, "readingWindowMinutes": None}
                      ).status_code in (400, 422)
    app.dependency_overrides.pop(require_super_admin_request, None)
    assert client.get("/v1/admin/activity-settings").status_code in (401, 403)


def test_the_all_projects_value_applies_where_a_project_has_none(journey):
    """Set inside a transaction that is rolled back: other tests share the all-Projects row."""
    with journey.engine.connect() as connection:
        transaction = connection.begin()
        try:
            settings.set_reading_window(connection, tenant_id=None, minutes=7, actor_id="sa-all")
            set_tenant_context(connection, journey.tenant_id)
            assert settings.reading_window(connection, journey.tenant_id) == {"minutes": 7, "scope": "ALL"}
            settings.set_reading_window(connection, tenant_id=journey.tenant_id, minutes=2, actor_id="sa-all")
            set_tenant_context(connection, journey.tenant_id)
            assert settings.reading_window(connection, journey.tenant_id) == {"minutes": 2, "scope": "PROJECT"}
            report = build_engagement(connection, tenant_id=journey.tenant_id, start_day=ist_today(),
                                      end_day=ist_today())
            assert report["readingWindowMinutes"] == 2 and report["idleThresholdMinutes"] == 2
            assert "the 2 minutes before it" in report["notes"][0] and "scrolling alone is not counted" in report["notes"][0]
        finally:
            transaction.rollback()


def test_the_app_reads_the_window_and_gets_it_back_with_every_send(journey):
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    _super_admin()
    client = TestClient(app, raise_server_exceptions=False)
    assert client.put("/v1/admin/activity-settings",
                      json={"tenantId": journey.tenant_id, "readingWindowMinutes": 4}).status_code == 200
    base = f"/p2/v1/tenants/{journey.tenant_id}"
    got = client.get(f"{base}/activity/settings")
    assert got.status_code == 200, got.text
    assert got.json() == {"readingWindowMinutes": 4, "scope": "PROJECT"}
    today = ist_today()
    minute = (datetime.now(UTC) + timedelta(hours=5, minutes=30)).hour * 60
    states = ["."] * 1440
    states[minute:minute + 3] = list("KAS")
    sent = client.post(f"{base}/activity", json={
        "date": today.isoformat(), "activeSeconds": 120, "idleSeconds": 60, "backgroundSeconds": 0,
        "minuteStates": "".join(states)})
    assert sent.status_code == 202, sent.text
    assert sent.json()["readingWindowMinutes"] == 4
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        stored = connection.execute(text(
            "SELECT minute_states FROM auditcore.p2_user_activity_daily WHERE tenant_id=:t AND actor_id=:a"),
            {"t": journey.tenant_id, "a": journey.actor_id}).scalar_one()
    assert stored[minute:minute + 3] == "KAS"
