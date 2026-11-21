"""Every service rests and runs by Security's shutdown switch (product owner 2026-11-21: "lets have
a tab in the verigence to have a full shutdown of DB"), not by a clock of its own."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as clock_time

import httpx
import pytest

from audit_core import sleep_window, system_switch

NIGHT = (clock_time(0, 30), clock_time(8, 0))


def _ist(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=sleep_window.IST).astimezone(UTC)


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _answer(**over: object) -> dict[str, object]:
    return {"closed": False, "opensAtUtc": None, "message": None, "resting": False, "restUntilUtc": None,
            "hoursFromIst": "00:30", "hoursToIst": "08:00", "manual": None, "manualSinceUtc": None,
            "manualUntilUtc": None, **over}


@pytest.fixture(autouse=True)
def _own_setting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sleep_window, "WINDOW", NIGHT)
    sleep_window.unfollow()
    yield
    sleep_window.unfollow()
    sleep_window.keep_awake(False)


def test_until_security_answers_the_services_own_setting_stands() -> None:
    assert sleep_window.followed() is None
    assert sleep_window.in_window(_ist(22, 3)) and not sleep_window.in_window(_ist(22, 14))
    assert sleep_window.seconds_until_wake(_ist(22, 7)) == 3600


def test_the_daily_hours_are_the_ones_security_says() -> None:
    assert sleep_window.follow(_answer(hoursFromIst="22:00", hoursToIst="07:30"))
    assert sleep_window.in_window(_ist(21, 23)) and sleep_window.in_window(_ist(22, 7, 29))
    assert not sleep_window.in_window(_ist(22, 7, 30)) and not sleep_window.in_window(_ist(22, 21, 59))
    assert sleep_window.seconds_until_wake(_ist(22, 7)) == 1800
    assert sleep_window.window_started_at(_ist(22, 3)) == _ist(21, 22)
    # No daily shutdown at all.
    assert sleep_window.follow(_answer(hoursFromIst=None, hoursToIst=None))
    assert not sleep_window.in_window(_ist(22, 3))


def test_a_shut_down_by_hand_in_the_evening_is_rested_through_like_the_night() -> None:
    six_pm, next_morning = _ist(21, 18), _ist(22, 8)
    assert sleep_window.follow(_answer(resting=True, manual="SHUT", manualSinceUtc=_z(six_pm),
                                       manualUntilUtc=_z(next_morning), restUntilUtc=_z(next_morning)))
    for at in (six_pm, _ist(21, 23, 59), _ist(22, 7, 59)):
        assert sleep_window.in_window(at) and sleep_window.asleep(at)
    assert not sleep_window.in_window(next_morning)
    assert sleep_window.seconds_until_wake(_ist(21, 20)) == 12 * 3600
    assert sleep_window.window_started_at(_ist(21, 20)) == six_pm
    # A service last heard from just before the shutdown is still "running" during it.
    now = _ist(21, 22)
    assert sleep_window.alive_cutoff(now, 180) == six_pm - timedelta(seconds=180)
    # Work in hand keeps the worker up, exactly as at night.
    sleep_window.keep_awake(True)
    assert sleep_window.in_window(now) and not sleep_window.asleep(now)


def test_a_shutdown_by_hand_that_runs_out_inside_the_daily_hours_goes_on_to_the_morning() -> None:
    assert sleep_window.follow(_answer(manual="SHUT", manualSinceUtc=_z(_ist(21, 18)), manualUntilUtc=_z(_ist(22, 2))))
    assert sleep_window.seconds_until_wake(_ist(22, 1)) == 7 * 3600


def test_start_now_at_night_ends_the_rest_until_the_morning_and_no_longer() -> None:
    three_am = _ist(22, 3)
    assert sleep_window.follow(_answer(manual="OPEN", manualSinceUtc=_z(three_am), manualUntilUtc=_z(_ist(22, 8))))
    for at in (three_am, _ist(22, 7, 59)):
        assert not sleep_window.in_window(at) and sleep_window.seconds_until_wake(at) == 0
    assert sleep_window.window_started_at(three_am) is None
    # The next night is shut as every night, without another answer.
    assert sleep_window.in_window(_ist(23, 0, 30))


def test_an_answer_that_is_not_one_changes_nothing() -> None:
    assert sleep_window.follow(_answer(hoursFromIst="22:00", hoursToIst="07:30"))
    before = sleep_window.followed()
    for bad in (None, "ok", [], {}, {"status": "live"}, _answer(hoursFromIst="ten", hoursToIst="07:30"),
                _answer(hoursFromIst="08:00", hoursToIst="08:00")):
        assert not sleep_window.follow(bad)
        assert sleep_window.followed() == before
    # A manual state without its end is not one: the hours alone.
    assert sleep_window.follow(_answer(manual="SHUT", manualUntilUtc=None))
    assert sleep_window.followed().manual is None
    assert sleep_window.follow(_answer(manual="CLOSED", manualUntilUtc=_z(_ist(22, 8))))
    assert sleep_window.followed().manual is None


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_asking_takes_securitys_answer_and_a_failure_leaves_the_state_as_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.test/")
    asked: list[str] = []

    def answers(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, json=_answer(hoursFromIst="22:00", hoursToIst="07:30"))

    assert system_switch.ask(_client(answers))
    assert asked == ["https://security.test/security/v1/system/status"]
    assert sleep_window.followed().hours == (clock_time(22, 0), clock_time(7, 30))

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    for failing in (down, lambda r: httpx.Response(503), lambda r: httpx.Response(200, text="<html>"),
                    lambda r: httpx.Response(200, json={"status": "live"})):
        assert not system_switch.ask(_client(failing))
        assert sleep_window.followed().hours == (clock_time(22, 0), clock_time(7, 30))


def test_a_process_with_no_security_address_or_told_never_to_sleep_keeps_its_own_setting(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECURITY_BASE_URL", raising=False)
    assert not system_switch.ask() and system_switch.start() is None
    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.test")
    monkeypatch.setattr(sleep_window, "WINDOW", None)
    assert system_switch.start() is None
    monkeypatch.setattr(sleep_window, "WINDOW", NIGHT)
    monkeypatch.setenv("VERIGENCE_SHUTDOWN_SWITCH", "off")
    assert system_switch.start() is None
