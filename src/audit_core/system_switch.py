"""Asking Verigence Security whether Verigence is shut (product owner 2026-11-21).

The shutdown switch is one piece of state held by Security (the daily hours, a
Super Admin's or Executive's "Shut down now" and "Start now"). This is the one
thread in a process that asks for it: once a minute, GET
/security/v1/system/status, which Security answers from memory with no database,
and the answer goes to ``sleep_window.follow``. Everything that rests at night
already goes by ``sleep_window``; nothing else changes.

It carries the schedule, not just "shut or open", so a service keeps the right
time by itself between two answers, and through a stretch in which Security
cannot be reached: the last answer stands (a Shut down or Start by hand runs out
at its own time), and before any answer the setting VERIGENCE_SLEEP_WINDOW_IST
does. A failure is never a reason to sleep or to wake.

``VERIGENCE_SHUTDOWN_SWITCH=off`` keeps a process on its own setting.
"""
from __future__ import annotations

import os
import threading

import httpx
import structlog

from audit_core import sleep_window

logger = structlog.get_logger(__name__)

ASK_EVERY_SECONDS = 60.0
_TIMEOUT_SECONDS = 5.0
_PATH = "/security/v1/system/status"

_started = threading.Lock()
_thread: threading.Thread | None = None


def enabled() -> bool:
    return os.environ.get("VERIGENCE_SHUTDOWN_SWITCH", "on").strip().lower() not in {"off", "false", "0", "no"}


def _base_url() -> str:
    return os.environ.get("SECURITY_BASE_URL", "").strip().rstrip("/")


def ask(client: httpx.Client | None = None) -> bool:
    """One question. True when Security's answer was taken; False otherwise, with
    the state left as it was."""
    base = _base_url()
    if not base:
        return False
    try:
        if client is None:
            response = httpx.get(f"{base}{_PATH}", timeout=_TIMEOUT_SECONDS)
        else:
            response = client.get(f"{base}{_PATH}", timeout=_TIMEOUT_SECONDS)
        if response.status_code != 200:
            return False
        return sleep_window.follow(response.json())
    except (httpx.HTTPError, ValueError):
        return False


def _run(stop: threading.Event) -> None:
    heard = False
    was_shut: bool | None = None
    while True:
        answered = ask()
        if answered != heard:
            heard = answered
            logger.info("shutdown_switch_heard" if answered else "shutdown_switch_not_heard")
        shut = sleep_window.in_window()
        if shut != was_shut:
            was_shut = shut
            state = sleep_window.followed()
            logger.info(
                "shutdown_switch_state",
                shut=shut,
                by_hand=state.manual if state is not None else None,
                wakes_in_seconds=round(sleep_window.seconds_until_wake()),
            )
        if stop.wait(ASK_EVERY_SECONDS):
            return


def start(stop: threading.Event | None = None) -> threading.Thread | None:
    """Start asking, once per process. None when switched off, when this process
    has no Security address, or when it was told never to sleep
    (VERIGENCE_SLEEP_WINDOW_IST=off): it then keeps its own setting."""
    global _thread
    if not enabled() or not _base_url() or sleep_window.WINDOW is None:
        return None
    with _started:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run, args=(stop or threading.Event(),), name="shutdown-switch",
                                       daemon=True)
            _thread.start()
        return _thread
