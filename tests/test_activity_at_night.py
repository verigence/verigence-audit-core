"""A tab left open must not keep the database up at night (2026-11-21).

On the night of 20 Nov every service slept and the database still ran all night: two tabs left
open sent their tally every minute and re-read the reading window every 13 minutes. Asleep, a
send with no click in the last ten minutes is left for the next one that is written, and the
reading window is told from memory -- with no connection taken at all. Real Postgres."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as clock_time

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import activity_at_night, sleep_window
from audit_core import activity_settings as settings
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_engine, get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.task_manager import ist_today

NIGHT = (clock_time(0, 30), clock_time(8, 0))
ALL_DAY = (clock_time(0, 0), clock_time(23, 59, 59))


def _ist(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 21, hour, minute, tzinfo=sleep_window.IST).astimezone(UTC)


@pytest.fixture(autouse=True)
def _nothing_remembered():
    activity_at_night.forget_windows()
    yield
    activity_at_night.forget_windows()
    app.dependency_overrides.clear()


# ── the rule ────────────────────────────────────────────────────────────────
def test_asleep_a_send_with_no_click_in_the_last_ten_minutes_is_left_for_later(monkeypatch):
    monkeypatch.setattr(sleep_window, "WINDOW", NIGHT)
    three_am = _ist(3)
    # The tab open since yesterday evening: today's tally has no active minute, yesterday's is old.
    assert activity_at_night.left_for_later(None, now=three_am)
    assert activity_at_night.left_for_later("2026-11-20T17:53:39.211Z", now=three_am)
    assert activity_at_night.left_for_later("not a time", now=three_am)
    assert activity_at_night.left_for_later("2026-11-21T02:55:00", now=three_am)        # no time zone
    # Someone at work at 3 am: written as by day, and for ten minutes after their last active minute.
    assert not activity_at_night.left_for_later((three_am - timedelta(seconds=20)).isoformat(), now=three_am)
    assert not activity_at_night.left_for_later((three_am - timedelta(minutes=9)).isoformat().replace("+00:00", "Z"),
                                                now=three_am)
    assert activity_at_night.left_for_later((three_am - timedelta(minutes=11)).isoformat(), now=three_am)


def test_by_day_and_with_no_sleep_window_every_send_is_written(monkeypatch):
    monkeypatch.setattr(sleep_window, "WINDOW", NIGHT)
    for at in (_ist(0, 29), _ist(8, 0), _ist(14), _ist(23, 59)):
        assert not activity_at_night.left_for_later(None, now=at)
    assert activity_at_night.left_for_later(None, now=_ist(0, 30))
    assert activity_at_night.left_for_later(None, now=_ist(7, 59))
    monkeypatch.setattr(sleep_window, "WINDOW", None)
    assert not activity_at_night.left_for_later(None, now=_ist(3))


def test_work_the_worker_still_has_in_hand_keeps_sends_written(monkeypatch):
    """The API never says so; a process that does is not asleep, so nothing is held back."""
    monkeypatch.setattr(sleep_window, "WINDOW", NIGHT)
    sleep_window.keep_awake(True)
    try:
        assert not activity_at_night.left_for_later(None, now=_ist(3))
    finally:
        sleep_window.keep_awake(False)


def test_the_reading_window_is_told_from_memory_only_asleep_and_only_to_who_was_told(monkeypatch):
    monkeypatch.setattr(sleep_window, "WINDOW", NIGHT)
    answer = {"readingWindowMinutes": 4, "scope": "PROJECT"}
    activity_at_night.remember_window("t1", "akansh", answer)
    assert activity_at_night.window_told("t1", "akansh", now=_ist(3)) == answer
    assert activity_at_night.window_told("t1", "akansh", now=_ist(14)) is None       # by day: read as always
    assert activity_at_night.window_told("t1", "someone-else", now=_ist(3)) is None  # never checked for them
    assert activity_at_night.window_told("t2", "akansh", now=_ist(3)) is None
    activity_at_night.forget_windows()
    assert activity_at_night.window_told("t1", "akansh", now=_ist(3)) is None


# ── the two calls an open tab makes ─────────────────────────────────────────
class _NoDatabase:
    """Stands for the engine: taking a connection from it is the failure."""

    def begin(self):
        raise AssertionError("the database was asked while asleep")

    connect = begin


class _NoSecurity:
    def check_user_permission(self, **_):
        raise AssertionError("Security was asked while asleep")


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _stored(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(
            "SELECT active_seconds, idle_seconds, background_seconds, minute_states "
            "FROM auditcore.p2_user_activity_daily WHERE tenant_id=:t AND actor_id=:a"),
            {"t": journey.tenant_id, "a": journey.actor_id}).mappings().one_or_none()


def test_asleep_an_open_tab_takes_no_connection_and_the_next_written_send_carries_the_night(journey, monkeypatch):
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}"
    today = ist_today().isoformat()

    # By day: the window is read and the tally written, as always.
    assert client.get(f"{base}/activity/settings").json() == {"readingWindowMinutes": 3, "scope": "DEFAULT"}
    states = ["."] * 1440
    states[0:3] = list("KAI")
    sent = client.post(f"{base}/activity", json={
        "date": today, "activeSeconds": 120, "idleSeconds": 60, "backgroundSeconds": 0,
        "minuteStates": "".join(states), "lastActiveAt": None})
    assert sent.status_code == 202 and sent.json() == {"recorded": True, "readingWindowMinutes": 3}

    # Asleep, the tab forgotten in the background: nothing may reach the database or Security.
    monkeypatch.setattr(sleep_window, "WINDOW", ALL_DAY)
    app.dependency_overrides[get_engine] = lambda: _NoDatabase()
    app.dependency_overrides[get_security_authorization_client] = lambda: _NoSecurity()
    states[3:60] = list("B" * 57)
    night = client.post(f"{base}/activity", json={
        "date": today, "activeSeconds": 120, "idleSeconds": 60, "backgroundSeconds": 3420,
        "minuteStates": "".join(states), "lastActiveAt": (datetime.now(UTC) - timedelta(hours=1)).isoformat()})
    assert night.status_code == 202, night.text
    assert night.json() == {"recorded": False}
    assert client.get(f"{base}/activity/settings").json() == {"readingWindowMinutes": 3, "scope": "DEFAULT"}
    assert _stored(journey)["background_seconds"] == 0

    # A click: written again, with every minute of the night in it.
    app.dependency_overrides.pop(get_engine)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    states[60] = "K"
    back = client.post(f"{base}/activity", json={
        "date": today, "activeSeconds": 180, "idleSeconds": 60, "backgroundSeconds": 3420,
        "minuteStates": "".join(states), "lastActiveAt": datetime.now(UTC).isoformat().replace("+00:00", "Z")})
    assert back.status_code == 202 and back.json()["recorded"] is True
    stored = _stored(journey)
    assert (stored["active_seconds"], stored["background_seconds"]) == (180, 3420)
    assert stored["minute_states"][0:61] == "KAI" + "B" * 57 + "K"


def test_asleep_someone_not_yet_told_the_window_reads_it_once_and_a_change_is_read_again(journey, monkeypatch):
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    monkeypatch.setattr(sleep_window, "WINDOW", ALL_DAY)
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/p2/v1/tenants/{journey.tenant_id}/activity/settings"
    assert client.get(url).json() == {"readingWindowMinutes": 3, "scope": "DEFAULT"}     # read once
    app.dependency_overrides[get_engine] = lambda: _NoDatabase()
    assert client.get(url).json() == {"readingWindowMinutes": 3, "scope": "DEFAULT"}     # then from memory
    app.dependency_overrides.pop(get_engine)

    with journey.engine.begin() as connection:
        settings.set_reading_window(connection, tenant_id=journey.tenant_id, minutes=6, actor_id="sa-night")
    assert client.get(url).json() == {"readingWindowMinutes": 6, "scope": "PROJECT"}


def test_a_change_that_is_rolled_back_or_read_before_its_commit_does_not_stay_in_memory(journey, monkeypatch):
    monkeypatch.setattr(sleep_window, "WINDOW", ALL_DAY)
    old = {"readingWindowMinutes": 3, "scope": "DEFAULT"}
    with journey.engine.connect() as connection:
        transaction = connection.begin()
        settings.set_reading_window(connection, tenant_id=journey.tenant_id, minutes=6, actor_id="sa-night")
        # A tab asks before the change is committed and is told (and remembered with) the old value.
        activity_at_night.remember_window(journey.tenant_id, journey.actor_id, old)
        assert activity_at_night.window_told(journey.tenant_id, journey.actor_id) == old
        transaction.commit()
    assert activity_at_night.window_told(journey.tenant_id, journey.actor_id) is None
