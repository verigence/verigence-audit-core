"""A tab left open, and the night's sleep (2026-11-21).

The app sends the day's tally of every open tab once a minute, and asks for the reading window
again each time its sign-in is renewed (about every 13 minutes) -- whether or not anyone is
there. On the night of 20 Nov the worker, Document Intelligence and myHR all slept as planned
and the database still ran for all 7.5 hours of the window: two tabs left open in the
background (one until 2:19 am, one until morning) were the only callers, and each send was a
transaction. One forgotten tab is enough to keep the database up, so nothing was saved.

Inside the sleep window (audit_core.sleep_window):

* a send with no click, tap or key in the last ten minutes is not written. Nothing is lost by
  that: the tally is the whole day's and the browser keeps it (today and yesterday), so the next
  send that is written -- after a click, or the first one after the window -- carries every
  minute of the night. Yesterday's tally was final at midnight and written before the window
  opened;
* the reading window is answered from what this process last told that person, when it has
  told them. A change made by a Super Admin forgets all of it.

A person at work at night is unaffected: their sends are written as by day, and the database is
up because they are using it. Outside the window nothing here applies.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from audit_core import sleep_window

# After the last active minute the sends go on being written this long, so the minutes of the
# reading window after a click (1 to 10) and the first idle ones are on file before it stops.
FRESH = timedelta(minutes=10)
_MOST_REMEMBERED = 5000

_lock = threading.Lock()
_told: dict[tuple[str, str], dict[str, Any]] = {}


def left_for_later(last_active_at: str | None, *, now: datetime | None = None) -> bool:
    """Whether a send is not to be written now: the system is asleep and the tally shows no
    active minute in the last ten. ``last_active_at`` is the tally's own (the app's clock); one
    that cannot be read is taken as no activity."""
    now = now or datetime.now(UTC)
    if not sleep_window.asleep(now):
        return False
    try:
        at = datetime.fromisoformat(str(last_active_at)) if last_active_at else None
    except ValueError:
        at = None
    if at is None or at.tzinfo is None:
        return True
    return at < now - FRESH


def remember_window(tenant_id: str, actor_id: str, window: dict[str, Any]) -> None:
    """What a person was just told the reading window is (read from the database, after the
    permission check)."""
    with _lock:
        if len(_told) >= _MOST_REMEMBERED:
            _told.clear()
        _told[(tenant_id, actor_id)] = dict(window)


def window_told(tenant_id: str, actor_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The reading window this person was last told, while the system is asleep; None by day
    (it is read as always) and for someone not told since this process started or since the
    last change."""
    if not sleep_window.asleep(now):
        return None
    with _lock:
        told = _told.get((tenant_id, actor_id))
    return dict(told) if told is not None else None


def forget_windows() -> None:
    """The reading window was changed: everyone reads it again."""
    with _lock:
        _told.clear()
