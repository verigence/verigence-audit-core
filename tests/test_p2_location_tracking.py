"""The phone's location in the background and readings sent later (product owner,
2026-11-16): "offline check for the location on the mobile device ... though app may
be in background or may be killed". Everyone who marks attendance on the phone is
tracked from check-in to check-out, within 10:00-19:30 IST. Real Postgres; Security
is a double."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import uc03_p2_location_checks as checks
from audit_core import uc03_p2_location_tracking as tracking
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_object_keys import IST

OUTLET = (20.4900, 85.8800)
NEARBY = (20.4920, 85.8800)  # ~222 m
FAR = (20.5900, 85.8800)  # ~11 km


def _ist(hour: int, minute: int = 0, day: int = 16) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


# --------------------------------------------------------------------------- pure rules


def _cmd(**extra):
    return checks.LocationCheckCommand(outcome="LOCATED", latitude=NEARBY[0], longitude=NEARBY[1], accuracyM=20,
                                       source="NATIVE", deviceType="ANDROID", **extra)


def test_a_queued_reading_counts_when_it_was_read():
    now = _ist(15)
    assert checks.check_moment(_cmd(), now) == now
    # Not queued: the device's own time is kept but the check counts now.
    assert checks.check_moment(_cmd(capturedAt=_ist(11)), now) == now
    assert checks.check_moment(_cmd(capturedAt=_ist(11), queued=True), now) == _ist(11)
    # Older than 48 hours: refused. A clock ahead of the server's: now.
    assert checks.check_moment(_cmd(capturedAt=now - timedelta(hours=49), queued=True), now) is None
    assert checks.check_moment(_cmd(capturedAt=now + timedelta(minutes=30), queued=True), now) == now
    with pytest.raises(ValueError):
        _cmd(queued=True)


def test_hours_on_dealguard_issues_and_gaps():
    states = ["."] * 1440
    for minute in (9 * 60 + 30, 10 * 60 + 5, 14 * 60 + 59, 19 * 60 + 40):
        states[minute] = "K"
    states[16 * 60 + 10] = "I"  # idle is not use
    # 09:30 and 19:40 are outside the window.
    assert checks.window_hours_of("".join(states)) == {10, 14}
    assert checks.window_hours_of(None) == set()

    assert checks.reading_issue({"UNCLEAR": 5, "AT_OUTLET": 2}, 7) == checks.DESKTOP_NO_GPS
    assert checks.reading_issue({"DENIED": 1, "UNAVAILABLE": 3, "AT_OUTLET": 2}, 6) == checks.LOCATION_OFF
    assert checks.reading_issue({"UNCLEAR": 2}, 2) is None  # too few to say
    assert checks.reading_issue({"UNCLEAR": 2, "AT_OUTLET": 3}, 5) is None

    assert checks.longest_gap_minutes([_ist(10), _ist(10, 20), _ist(11, 45)]) == 85
    assert checks.longest_gap_minutes([_ist(10)]) is None


def test_tracking_until_is_the_days_window_end():
    assert tracking.tracking_until(_ist(9, 15)) == _ist(19, 30).astimezone(UTC)
    assert tracking.key_hash("abc") == tracking.key_hash("abc") != tracking.key_hash("abd")


# --------------------------------------------------------------------------- with the database


@pytest.fixture
def setup(monkeypatch):
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="trk")
    tl = f"trk-tl-{uuid4().hex[:8]}"
    executive = f"trk-ex-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.dealer_outlets SET latitude=:lat, longitude=:lon, outlet_name='CDA' "
                 "WHERE tenant_id=:t AND outlet_id=:o"),
            {"lat": OUTLET[0], "lon": OUTLET[1], "t": journey.tenant_id, "o": journey.outlet_id},
        )
        for actor, role in ((tl, "TL"), (executive, "EXECUTIVE")):
            connection.execute(
                text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code) "
                     "VALUES (:t, :a, :r)"),
                {"t": journey.tenant_id, "a": actor, "r": role},
            )
    acting = {"subject": journey.actor_id}
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=acting["subject"])
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    monkeypatch.setattr(checks, "in_window", lambda now: True)
    monkeypatch.setattr(tracking, "in_window", lambda now: True)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield SimpleNamespace(journey=journey, engine=engine, client=client, acting=acting, tl=tl,
                              executive=executive, tenant=journey.tenant_id)
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def _reading(lat_lon, at: datetime, client_id: str | None = None, **extra):
    return {"outcome": "LOCATED", "latitude": lat_lon[0], "longitude": lat_lon[1], "accuracyM": 15,
            "capturedAt": at.astimezone(UTC).isoformat(), "queued": True, "source": "NATIVE",
            "deviceType": "ANDROID", "clientCheckId": client_id or uuid4().hex, **extra}


def _rows(s):
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.tenant)
        return [dict(r) for r in connection.execute(
            text("SELECT * FROM auditcore.p2_location_checks WHERE tenant_id=:t ORDER BY checked_at_utc"),
            {"t": s.tenant},
        ).mappings()]


def _start(s, actor: str, now: datetime, tenant: str | None = None) -> tracking.TrackingStart:
    with s.engine.begin() as connection:
        return tracking.start_tracking(connection, actor_id=actor,
                                       command=tracking.TrackingStartCommand(tenantId=tenant), now=now)


def _send(s, key: str, readings: list[dict], now: datetime):
    with s.engine.begin() as connection:
        return tracking.record_readings(connection, tenant_id=s.tenant, key=key,
                                        readings=tracking.TrackingReadings(checks=readings), now=now)


def test_a_dealguard_check_kept_offline_is_logged_once_at_its_own_time(setup):
    s = setup
    base = f"/p2/v1/tenants/{s.tenant}/location-checks"
    read_at = datetime.now(UTC) - timedelta(hours=3)
    body = _reading(FAR, read_at, client_id="offline-0001", source="BROWSER", deviceType="MOBILE_WEB")
    first = s.client.post(base, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["recorded"] is True and first.json()["status"] == "AWAY"
    again = s.client.post(base, json=body)
    assert again.json()["duplicate"] is True and again.json()["checkId"] == first.json()["checkId"]
    [row] = _rows(s)
    assert row["queued"] is True and row["check_trigger"] == "FOREGROUND"
    assert abs((row["checked_at_utc"] - read_at).total_seconds()) < 1
    assert row["received_at_utc"] > row["checked_at_utc"]

    too_old = s.client.post(base, json=_reading(FAR, datetime.now(UTC) - timedelta(hours=50))).json()
    assert too_old["recorded"] is False and too_old["reason"] == "TOO_OLD"


def test_everyone_who_marks_attendance_gets_a_key_and_their_readings_are_logged(setup):
    s = setup
    # A TL is tracked too, judged against their outlets in any role (Project-wide here).
    start = _start(s, s.tl, _ist(9, 40))
    assert start.tracking is True and start.tenantId == s.tenant and start.key
    assert start.trackingUntilUtc == _ist(19, 30).astimezone(UTC)
    assert start.path == f"/p2/v1/tenants/{s.tenant}/location-tracking/checks"
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.tenant)
        stored = connection.execute(text("SELECT key_hash FROM auditcore.p2_location_tracking_keys "
                                         "WHERE tenant_id=:t AND actor_id=:a"), {"t": s.tenant, "a": s.tl}).scalar_one()
    assert stored == tracking.key_hash(start.key) and start.key not in stored

    sent = _send(s, start.key, [_reading(NEARBY, _ist(10, 10)), _reading(FAR, _ist(10, 30))], now=_ist(10, 31))
    assert [r.status for r in sent.results] == ["AT_OUTLET", "AWAY"]
    assert sent.trackingActive is True
    rows = _rows(s)
    assert {r["check_trigger"] for r in rows} == {"BACKGROUND"} and {r["actor_id"] for r in rows} == {s.tl}
    assert rows[1]["distance_m"] > 10_000

    # Over the API: no sign-in token, the key in its header.
    path = start.path
    assert s.client.post(path, json={"checks": [_reading(NEARBY, datetime.now(UTC))]}).status_code == 401
    wrong = s.client.post(path, json={"checks": [_reading(NEARBY, datetime.now(UTC))]},
                          headers={tracking.KEY_HEADER: "not-a-key"})
    assert wrong.status_code == 401 and wrong.json()["code"] == "KEY_INVALID"


def test_check_out_stops_the_key_but_keeps_what_was_read_before(setup):
    s = setup
    start = _start(s, s.journey.actor_id, _ist(10))
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.tenant)
        connection.execute(text("UPDATE auditcore.p2_location_tracking_keys SET revoked_at_utc=:at "
                                "WHERE tenant_id=:t"), {"at": _ist(15), "t": s.tenant})
    # Sent at 16:00 after a dead spot: the 14:40 reading counts, the 15:20 one does not.
    sent = _send(s, start.key, [_reading(NEARBY, _ist(14, 40)), _reading(NEARBY, _ist(15, 20))], now=_ist(16))
    assert [r.recorded for r in sent.results] == [True, False]
    assert sent.results[1].reason == "TRACKING_STOPPED" and sent.trackingActive is False
    # Still accepted for 48 hours after the window, then refused.
    assert _send(s, start.key, [_reading(NEARBY, _ist(14, 50))], now=_ist(19, 0, day=18)) is not None
    assert _send(s, start.key, [_reading(NEARBY, _ist(14, 55))], now=_ist(19, 31, day=18)) is None


def test_a_new_check_in_stops_the_other_phone_and_no_key_after_the_window(setup):
    s = setup
    first = _start(s, s.journey.actor_id, _ist(10))
    second = _start(s, s.journey.actor_id, _ist(11))
    assert _send(s, first.key, [_reading(NEARBY, _ist(11, 30))], now=_ist(11, 31)).trackingActive is False
    assert _send(s, second.key, [_reading(NEARBY, _ist(11, 40))], now=_ist(11, 41)).trackingActive is True
    late = _start(s, s.journey.actor_id, _ist(19, 45))
    assert late.tracking is False and late.reason == "WINDOW_OVER"
    stranger = _start(s, f"nobody-{uuid4().hex[:6]}", _ist(10))
    assert stranger.tracking is False and stranger.reason == "NO_PROJECT"


def test_the_days_log_names_every_pc_and_why(setup):
    s = setup
    day = date(2026, 11, 16)
    start = _start(s, s.journey.actor_id, datetime.combine(day, datetime.min.time(), IST) + timedelta(hours=10))
    moments = [datetime.combine(day, datetime.min.time(), IST) + timedelta(hours=10, minutes=m) for m in (5, 25, 140)]
    _send(s, start.key, [_reading(NEARBY, m) for m in moments], now=moments[-1] + timedelta(minutes=1))
    other_pc = f"trk-pc2-{uuid4().hex[:6]}"
    states = ["."] * 1440
    states[11 * 60 + 3] = "K"
    with s.engine.begin() as connection:
        set_tenant_context(connection, s.tenant)
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, :a, 'PC', :d, :o)"),
            {"t": s.tenant, "a": other_pc, "d": s.journey.dealer_id, "o": s.journey.outlet_id},
        )
        columns = {r for r in connection.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='auditcore' "
            "AND table_name='p2_user_activity_daily'")).scalars()}
        values = {"tenant_id": s.tenant, "actor_id": other_pc, "activity_date": day,
                  "minute_states": "".join(states)}
        if "client_kind" in columns:
            values["client_kind"] = "WEB"
        connection.execute(
            text(f"INSERT INTO auditcore.p2_user_activity_daily ({', '.join(values)}) "
                 f"VALUES ({', '.join(':' + k for k in values)})"),
            values,
        )
    s.acting["subject"] = s.executive
    log = s.client.get(f"/p2/v1/tenants/{s.tenant}/location-checks", params={"day": day.isoformat()})
    assert log.status_code == 200, log.text
    body = log.json()
    people = {p["actorId"]: p for p in body["people"]}
    tracked = people[s.journey.actor_id]
    assert tracked["background"] is True and tracked["checks"] == 3
    assert tracked["longestGapMinutes"] == 115 and tracked["issue"] == "TRACKING_GAP"
    assert tracked["items"][0]["trigger"] == "BACKGROUND" and tracked["items"][0]["queued"] is True
    idle = people[other_pc]
    assert idle["checks"] == 0 and idle["hoursOnDealGuard"] == 1 and idle["issue"] == "NO_CHECK_RECEIVED"
    assert body["totals"]["pcsInProject"] == 2 and body["totals"]["notChecked"] == 1
    assert body["totals"]["background"] == 1 and body["trackingIntervalMinutes"] == 20


def test_the_away_report_names_people_who_cannot_be_placed(setup):
    s = setup
    base = f"/p2/v1/tenants/{s.tenant}/location-checks"
    for _ in range(3):
        desktop = {"outcome": "LOCATED", "latitude": FAR[0], "longitude": FAR[1], "accuracyM": 30000,
                   "source": "BROWSER", "deviceType": "WEB"}
        assert s.client.post(base, json=desktop).json()["status"] == "UNCLEAR"
    s.acting["subject"] = s.executive
    report = s.client.get(f"{base}:away").json()
    assert report["byPc"] == []
    [unplaced] = report["unplaced"]
    assert unplaced["actorId"] == s.journey.actor_id and unplaced["issue"] == "DESKTOP_NO_GPS"
    assert unplaced["unclear"] == 3 and unplaced["checks"] == 3
