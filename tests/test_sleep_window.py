"""The night's sleep (product owner 2026-11-13: "we can make system sleep from
12:30 am to 8:00 am"): inside the window nothing asks the database anything on
its own clock, and no work is left undone for it."""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core import platform_health, sleep_window
from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context

NIGHT = (clock_time(0, 30), clock_time(8, 0))
ALL_DAY = (clock_time(0, 0), clock_time(23, 59, 59, 999999))


def _ist(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 14, hour, minute, tzinfo=sleep_window.IST).astimezone(UTC)


@pytest.fixture(autouse=True)
def _awake_afterwards(monkeypatch):
    monkeypatch.setattr(worker, "_night", {"rest_until": 0.0, "up_until": 0.0, "looked_at": 0.0})
    monkeypatch.setattr(worker, "_known_tenants", [])
    yield
    sleep_window.keep_awake(False)


# ── the window ──────────────────────────────────────────────────────────────

def test_the_window_is_half_past_midnight_to_eight_in_the_morning_ist(monkeypatch):
    monkeypatch.setattr(sleep_window, "WINDOW", sleep_window.parse_window(sleep_window.DEFAULT_WINDOW))
    assert sleep_window.WINDOW == NIGHT
    assert not sleep_window.in_window(_ist(0, 29))
    assert sleep_window.in_window(_ist(0, 30))
    assert sleep_window.in_window(_ist(7, 59))
    assert not sleep_window.in_window(_ist(8, 0))
    assert not sleep_window.in_window(_ist(21, 0))  # the Drive run's hour
    assert sleep_window.seconds_until_wake(_ist(7, 0)) == 3600
    assert sleep_window.seconds_until_wake(_ist(12, 0)) == 0
    assert sleep_window.window_started_at(_ist(3, 0)) == _ist(0, 30)
    assert sleep_window.window_started_at(_ist(12, 0)) is None


def test_a_window_may_run_over_midnight(monkeypatch):
    monkeypatch.setattr(sleep_window, "WINDOW", sleep_window.parse_window("22:00-06:00"))
    assert sleep_window.in_window(_ist(23, 0)) and sleep_window.in_window(_ist(5, 59))
    assert not sleep_window.in_window(_ist(6, 0)) and not sleep_window.in_window(_ist(21, 59))
    assert sleep_window.seconds_until_wake(_ist(23, 0)) == 7 * 3600
    assert sleep_window.window_started_at(_ist(5, 0)) == _ist(22, 0) - timedelta(days=1)


def test_off_never_sleeps_and_a_typing_mistake_is_the_default_window():
    assert sleep_window.parse_window("off") is None
    assert sleep_window.parse_window("00:30 - 08:00") == NIGHT
    assert sleep_window.parse_window("half past twelve") == NIGHT
    assert sleep_window.parse_window(None) == NIGHT


def test_a_process_with_the_nights_work_in_hand_is_not_asleep(monkeypatch):
    monkeypatch.setattr(sleep_window, "WINDOW", NIGHT)
    assert sleep_window.asleep(_ist(2, 0))
    sleep_window.keep_awake(True)
    assert not sleep_window.asleep(_ist(2, 0))
    sleep_window.keep_awake(False)
    assert not sleep_window.asleep(_ist(12, 0))


def test_a_service_last_heard_from_as_the_window_opened_is_running(monkeypatch):
    """System Health: asleep a service does not beat; one that stopped beating
    an hour before the window opened is down all the same."""
    monkeypatch.setattr(sleep_window, "WINDOW", NIGHT)
    cutoff = sleep_window.alive_cutoff(_ist(4, 0), 180)
    assert _ist(0, 28) >= cutoff > _ist(0, 26)
    assert _ist(23, 30) - timedelta(days=1) < cutoff
    assert sleep_window.alive_cutoff(_ist(12, 0), 180) == _ist(12, 0) - timedelta(seconds=180)


# ── the worker ──────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Stop:
    """A stop event whose waits pass at once and move the clock."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.clock.now += seconds
        return False

    def is_set(self) -> bool:
        return False


@pytest.fixture
def night(monkeypatch):
    """Inside the window, with the database look replaced by ``night.due_in``."""
    clock = _Clock()
    looks: list[float] = []

    class Night:
        due_in: float | None = None
        scheduled = False

    def look(_engine):
        looks.append(clock.now)
        if isinstance(Night.due_in, Exception):
            raise Night.due_in
        return Night.due_in

    monkeypatch.setattr(sleep_window, "WINDOW", ALL_DAY)
    monkeypatch.setattr(worker.time, "monotonic", clock)
    monkeypatch.setattr(worker, "_night_work_due_in", look)
    monkeypatch.setattr(worker, "_scheduled_run_due", lambda: Night.scheduled)
    Night.clock, Night.looks, Night.stop = clock, looks, _Stop(clock)
    return Night


def test_with_nothing_to_do_the_worker_sleeps_and_asks_the_database_nothing(night):
    assert worker._night_rest(None, night.stop) is True
    assert sleep_window.asleep()
    for _ in range(100):  # 100 turns of the loop: 5 seconds each, no look
        assert worker._night_rest(None, night.stop) is True
    assert len(night.looks) == 1
    assert set(night.stop.waits) == {worker._NIGHT_NAP_SECONDS}


def test_once_an_hour_it_is_up_for_a_pass_and_looks_again(night):
    assert worker._night_rest(None, night.stop) is True
    night.clock.now += worker._NIGHT_LOOK_SECONDS
    assert worker._night_rest(None, night.stop) is False  # up: the sweeps and side loops run once
    assert not sleep_window.asleep()
    night.clock.now += worker._NIGHT_UP_SECONDS
    assert worker._night_rest(None, night.stop) is True   # looked, nothing: asleep again
    assert sleep_window.asleep() and len(night.looks) == 2


def test_work_waiting_keeps_the_worker_up(night):
    night.due_in = 0.0
    assert worker._night_rest(None, night.stop) is False
    assert not sleep_window.asleep()
    assert worker._night_rest(None, night.stop) is False  # the next idle turn: no second look yet
    assert len(night.looks) == 1
    night.clock.now += worker._NIGHT_BUSY_LOOK_SECONDS
    night.due_in = None                                    # the reading is through
    assert worker._night_rest(None, night.stop) is True
    assert sleep_window.asleep()


def test_a_retry_due_in_the_window_is_woken_for_at_its_time(night):
    night.due_in = 240.0
    assert worker._night_rest(None, night.stop) is True
    assert worker._night["rest_until"] == night.looks[0] + 240.0


def test_a_run_of_the_night_that_is_due_gets_the_worker_up(night):
    assert worker._night_rest(None, night.stop) is True
    night.scheduled = True                                 # 02:00: the day-end sweep
    assert worker._night_rest(None, night.stop) is False
    assert not sleep_window.asleep()
    night.scheduled = False                                # it ran
    night.clock.now += worker._NIGHT_UP_SECONDS
    assert worker._night_rest(None, night.stop) is True


def test_not_knowing_is_a_reason_to_stay_up(night):
    night.due_in = RuntimeError("database not reachable")
    assert worker._night_rest(None, night.stop) is False
    assert not night.stop.waits


def test_by_day_the_worker_never_sleeps(night, monkeypatch):
    assert worker._night_rest(None, night.stop) is True
    monkeypatch.setattr(sleep_window, "WINDOW", None)      # eight o'clock
    assert worker._night_rest(None, night.stop) is False
    assert worker._night == {"rest_until": 0.0, "up_until": 0.0, "looked_at": 0.0}
    assert not sleep_window.asleep()


def test_a_loop_beside_the_main_one_skips_its_turn_asleep(monkeypatch):
    monkeypatch.setattr(sleep_window, "WINDOW", ALL_DAY)
    monkeypatch.setattr(worker, "_NIGHT_NAP_SECONDS", 0.01)
    assert worker._asleep_nap() is True
    sleep_window.keep_awake(True)
    assert worker._asleep_nap() is False


def test_no_heartbeat_is_written_asleep(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("asked the database while asleep")

    monkeypatch.setattr(sleep_window, "WINDOW", ALL_DAY)
    monkeypatch.setattr(platform_health, "sample_resources", fail)
    monkeypatch.setattr(platform_health, "beat", fail)
    monkeypatch.setattr(platform_health, "sample_db_minute", fail)
    platform_health._tick(None, "verigence-audit-core", platform_health.BEAT_EVERY_SAMPLES, api=True)


# ── against the database ────────────────────────────────────────────────────

@pytest.fixture
def journey(monkeypatch):
    engine = database_engine()
    created = create_p2_journey(engine, prefix="sleep")
    monkeypatch.setattr(worker, "_active_tenants", lambda _engine: [created.tenant_id])
    monkeypatch.setattr(worker, "_maybe_sweep", lambda *_a, **_k: None)
    try:
        yield created
    finally:
        worker._inflight.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _enqueue(journey, *, retry_in_seconds: int | None = None) -> str:
    key = f"test:{uuid4().hex}"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        worker._enqueue(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                        work_type="DOCUMENT_INGEST", work_key=key, payload={}, correlation_id=None)
        if retry_in_seconds is not None:
            connection.execute(
                text("UPDATE auditcore.p2_work_queue SET work_status='RETRY_WAIT', "
                     "next_attempt_at_utc=now() + make_interval(secs => :s) WHERE tenant_id=:t AND work_key=:k"),
                {"s": retry_in_seconds, "t": journey.tenant_id, "k": key})
    return key


def test_the_look_says_when_there_is_next_something_to_do(journey):
    assert worker._night_work_due_in(journey.engine) is None
    assert worker._known_tenants == [journey.tenant_id]
    _enqueue(journey, retry_in_seconds=600)
    assert 590 < worker._night_work_due_in(journey.engine) <= 600
    _enqueue(journey)
    assert worker._night_work_due_in(journey.engine) == 0


def test_work_on_the_way_as_the_window_opens_is_finished_before_the_worker_sleeps(journey, monkeypatch):
    key = _enqueue(journey)
    done: list[str] = []
    stop = threading.Event()

    def process(_engine, work):
        done.append(work.work_key)

    def asleep_then_stop(*_a, **_k):
        stop.set()  # the first sleep: end the test here

    monkeypatch.setattr(sleep_window, "WINDOW", ALL_DAY)
    monkeypatch.setattr(worker, "process_work", process)
    monkeypatch.setattr(worker, "_POLL_SECONDS", 0.05)
    monkeypatch.setattr(worker, "_NIGHT_BUSY_LOOK_SECONDS", 0.0)
    real_rest = worker._night_rest

    def rest(engine, stop_event):
        slept = real_rest(engine, stop_event)
        if slept:
            asleep_then_stop()
        return slept

    monkeypatch.setattr(worker, "_night_rest", rest)
    started = time.monotonic()
    worker.run_forever(journey.engine, stop=stop)
    assert done == [key]
    assert sleep_window.asleep()
    assert time.monotonic() - started < 10
