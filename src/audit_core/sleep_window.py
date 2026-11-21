"""The night's sleep (product owner 2026-11-13): "we can make system sleep from
12:30 am to 8:00 am because drive will have job which start works at 9:00 pm".

Why: the database (Neon) is billed for every hour it is running, and it stops
by itself only after five minutes in which nobody asked it anything. Until now
it never stopped: the worker asked for work every second, every service wrote
a heartbeat every minute and a dozen sweeps ran on their own clocks -- about
510 transactions a minute all night with nobody at work (12-13 Nov), and the
database billed half a compute unit for all 24 hours.

What sleeping means here: between the two times below (IST) nothing in this
service asks the database anything on its own clock. Nothing is switched off:

* a request from a person or another service is served as always (the
  database starts again by itself, in under a second);
* work already on the way when the window opens is finished first, and the
  night's own runs (nightly review, day-end sweep, data archive) still run at
  their times -- the worker is up for as long as they take, then sleeps again
  (uc03_p2_worker._night_rest);
* at the end of the window everything carries on where it stopped.

``VERIGENCE_SLEEP_WINDOW_IST`` sets the window ("00:30-08:00"); "off" means
never sleep.

The shutdown switch (product owner 2026-11-21: "lets have a tab in the
verigence to have a full shutdown of DB"). The hours above were a setting of
each service. They are now one piece of state held by Verigence Security, which
a Super Admin or Executive changes from the System shutdown tab: the daily
hours, a "Shut down now" (shut to the next daily opening) and a "Start now".
Every service asks Security for it once a minute (audit_core.system_switch; an
answer from memory there, no database) and hands the answer to ``follow``.
From then on "the window" in this module is that state: a shutdown by hand at
six in the evening is rested through exactly as the night is, with work in
hand finished first, and a Start at three at night ends the rest within the
minute. Until the first answer, and with ``follow`` never called (tests), the
window is the setting above.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_WINDOW = "00:30-08:00"

Window = tuple[clock_time, clock_time]


def parse_window(value: str | None) -> Window | None:
    """``"00:30-08:00"`` as two times of day (IST); None for "off". Anything
    unreadable is the default window: a typing mistake must not keep the
    database up all night unnoticed, nor put the system to sleep by day."""
    raw = (value or "").strip().lower()
    if raw in {"off", "none", "never", "false", "0"}:
        return None
    for candidate in (raw, DEFAULT_WINDOW):
        start_text, _, end_text = candidate.partition("-")
        try:
            start = clock_time.fromisoformat(start_text.strip())
            end = clock_time.fromisoformat(end_text.strip())
        except ValueError:
            continue
        if start != end:
            return start, end
    return None


WINDOW: Window | None = parse_window(os.environ.get("VERIGENCE_SLEEP_WINDOW_IST", DEFAULT_WINDOW))

# Set by a process that still has the night's work in hand (the worker); a
# process that never sets it sleeps by the clock alone (the API).
_working = threading.Event()


SHUT = "SHUT"
OPEN = "OPEN"


@dataclass(frozen=True)
class Followed:
    """Security's shutdown switch as last heard: the daily hours (None: no daily
    shutdown) and a Shut down or Start by hand, in force from ``since`` until
    ``until``."""

    hours: Window | None
    manual: str | None = None
    since: datetime | None = None
    until: datetime | None = None


_followed: Followed | None = None
_followed_lock = threading.Lock()


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC) if value else None
    except ValueError:
        return None


def follow(status: object) -> bool:
    """Take Security's answer (GET /security/v1/system/status) as the state to go
    by. False, and nothing changes, for an answer that is not one: a service
    must not start or stop sleeping on a page that happened to say 200."""
    global _followed
    if not isinstance(status, dict) or "resting" not in status:
        return False
    hours: Window | None = None
    start_text, end_text = status.get("hoursFromIst"), status.get("hoursToIst")
    if start_text or end_text:
        try:
            start, end = clock_time.fromisoformat(str(start_text)), clock_time.fromisoformat(str(end_text))
        except ValueError:
            return False
        if start == end:
            return False
        hours = (start, end)
    manual = status.get("manual")
    until = _when(status.get("manualUntilUtc"))
    if manual not in {SHUT, OPEN} or until is None:
        manual, until = None, None
    since = _when(status.get("manualSinceUtc")) if manual else None
    with _followed_lock:
        _followed = Followed(hours=hours, manual=manual, since=since, until=until)
    return True


def unfollow() -> None:
    """Back to the setting alone (tests; a service told not to follow the switch)."""
    global _followed
    with _followed_lock:
        _followed = None


def followed() -> Followed | None:
    with _followed_lock:
        return _followed


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(IST)


def _hours() -> Window | None:
    state = followed()
    return state.hours if state is not None else WINDOW


def _manual(now: datetime | None) -> Followed | None:
    """The Shut down or Start by hand in force at this moment, if any."""
    state = followed()
    if state is None or state.manual is None or state.until is None:
        return None
    return state if _now(now) < state.until else None


def _in_hours(now: datetime | None) -> bool:
    hours = _hours()
    if hours is None:
        return False
    start, end = hours
    local = _now(now).time()
    return start <= local < end if start < end else (local >= start or local < end)


def _next_opening(moment: datetime) -> datetime | None:
    """The next daily opening time after ``moment``."""
    hours = _hours()
    if hours is None:
        return None
    local = moment.astimezone(IST)
    wake = local.replace(hour=hours[1].hour, minute=hours[1].minute, second=0, microsecond=0)
    if wake <= local:
        wake += timedelta(days=1)
    return wake


def in_window(now: datetime | None = None) -> bool:
    """Whether Verigence is shut at this moment: a Shut down or Start by hand
    while it lasts, else the daily hours."""
    manual = _manual(now)
    return manual.manual == SHUT if manual is not None else _in_hours(now)


def window_started_at(now: datetime | None = None) -> datetime | None:
    """When the shutdown the clock is in began; None outside one."""
    if not in_window(now):
        return None
    manual = _manual(now)
    if manual is not None:
        return (manual.since or _now(now)).astimezone(UTC)
    local = _now(now)
    start = _hours()[0]  # type: ignore[index]  # in_window() is False without hours
    began = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if began > local:
        began -= timedelta(days=1)
    return began.astimezone(UTC)


def seconds_until_wake(now: datetime | None = None) -> float:
    """Seconds left of the shutdown; 0 outside one."""
    if not in_window(now):
        return 0.0
    local = _now(now)
    manual = _manual(now)
    wake = manual.until if manual is not None else _next_opening(local)  # type: ignore[union-attr]
    if manual is not None and wake is not None and _in_hours(wake):
        # A shutdown by hand that runs out inside the daily hours goes on to that day's opening.
        wake = _next_opening(wake)
    return max(0.0, (wake - local).total_seconds()) if wake is not None else 0.0


def keep_awake(working: bool) -> None:
    """The worker's word on whether it still has the night's work in hand."""
    if working:
        _working.set()
    else:
        _working.clear()


def asleep(now: datetime | None = None) -> bool:
    """Whether this process should leave the database alone right now: inside
    the window, and not in the middle of the night's work."""
    return in_window(now) and not _working.is_set()


def alive_cutoff(now: datetime, alive_seconds: float) -> datetime:
    """The oldest heartbeat that still says "running". By day a service beats
    every minute; asleep it does not beat at all, so during the window a
    service last heard from just before the window opened is running too."""
    cutoff = now - timedelta(seconds=alive_seconds)
    began = window_started_at(now)
    return cutoff if began is None else min(cutoff, began - timedelta(seconds=alive_seconds))
