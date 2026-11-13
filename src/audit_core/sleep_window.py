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
"""
from __future__ import annotations

import os
import threading
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


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(IST)


def in_window(now: datetime | None = None) -> bool:
    """Whether the clock is inside the sleep window."""
    if WINDOW is None:
        return False
    start, end = WINDOW
    local = _now(now).time()
    return start <= local < end if start < end else (local >= start or local < end)


def window_started_at(now: datetime | None = None) -> datetime | None:
    """When the window the clock is in began; None outside it."""
    if not in_window(now):
        return None
    local = _now(now)
    start = WINDOW[0]  # type: ignore[index]  # in_window() is False without a window
    began = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if began > local:
        began -= timedelta(days=1)
    return began.astimezone(UTC)


def seconds_until_wake(now: datetime | None = None) -> float:
    """Seconds left of the window; 0 outside it."""
    if not in_window(now):
        return 0.0
    local = _now(now)
    end = WINDOW[1]  # type: ignore[index]
    wake = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if wake <= local:
        wake += timedelta(days=1)
    return (wake - local).total_seconds()


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
