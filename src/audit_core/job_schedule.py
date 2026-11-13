"""The time of each daily job, in one place a SuperAdmin can change.

Product owner, 13 Nov 2026: "create dashboard of all jobs which get executed for
Superadmin and allow him to change the time" and "lets move all jobs before
10:00 pm, so we can close all things by mid night and have system sleep".

A daily job runs once a day at its time (India time) for every live Project.
Its time is, in this order: the one a SuperAdmin set (platform_ops.job_schedules),
else the worker's environment variable where one existed before, else the
default below. The defaults are all before 10:00 pm: with the Drive run at
9:00 pm the night's work is through by midnight and the system sleeps
(audit_core.sleep_window).

The worker and the API each hold the times in memory and read the table again
once a minute while awake (platform_health._tick), so a change made on the page
is followed within a minute -- or, made at night, at the worker's next look.
Asleep nobody reads it: the times in memory stand.

The code that decides whether a job is due keeps its own clock in UTC
(uc03_p2_worker._nightly_review_due and the like); ``apply`` hands it the time.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import time as clock_time
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core import sleep_window

logger = structlog.get_logger(__name__)

_IST_OFFSET_MINUTES = 330
_OFF = {"", "off", "none"}


@dataclass(frozen=True)
class DailyJob:
    key: str
    name: str
    what: str
    default_ist: str
    run_name: str  # its mark in auditcore.p2_scheduled_runs: one per Project and day
    env_utc: str  # the worker variable that set its time (UTC) before this table
    can_be_off: bool = False


DAILY_JOBS: dict[str, DailyJob] = {
    job.key: job
    for job in (
        DailyJob(
            key="DAYEND_CLEANUP",
            name="Day-end clean-up",
            what=("Takes every page that ended failed after its retries off its journey. The file's "
                  "\"Document to resubmit\" task stays open for the PC. The PC's messages quote this time."),
            default_ist="21:30",
            run_name="P2_DAYEND_SWEEP",
            env_utc="P2_DAYEND_SWEEP_UTC",
        ),
        DailyJob(
            key="NIGHTLY_REVIEW",
            name="Nightly review",
            what=("Runs the Delivery checks again for every delivery in progress, so the checks that "
                  "depend on time (settlement, financier, resale, documents overdue) fire without a new "
                  "document; and brings in any page Document Intelligence has read that was not copied."),
            default_ist="21:30",
            run_name="P2_NIGHTLY_REVIEW",
            env_utc="P2_NIGHTLY_REVIEW_UTC",
        ),
        DailyJob(
            key="DATA_ARCHIVE",
            name="Data archive",
            what=("Moves old records (rule runs and the like) to the archive bucket, checks each batch "
                  "and only then deletes it from the database."),
            default_ist="21:50",
            run_name="P2_DATA_ARCHIVE",
            env_utc="P2_DATA_ARCHIVE_UTC",
            can_be_off=True,
        ),
    )
}


def parse_hhmm(value: Any) -> str | None:
    """``HH:MM`` (24 hour) from what a person typed or the table holds; None when it is not a time."""
    if isinstance(value, clock_time):
        return f"{value.hour:02d}:{value.minute:02d}"
    hour, sep, minute = str(value or "").strip().partition(":")
    minute = minute.partition(":")[0]
    if sep and hour.isdigit() and minute.isdigit() and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59:
        return f"{int(hour):02d}:{int(minute):02d}"
    return None


def _shift(hhmm: str, minutes: int) -> str:
    hour, _, minute = hhmm.partition(":")
    total = (int(hour) * 60 + int(minute) + minutes) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def ist_to_utc(hhmm: str) -> str:
    return _shift(hhmm, -_IST_OFFSET_MINUTES)


def utc_to_ist(hhmm: str) -> str:
    return _shift(hhmm, _IST_OFFSET_MINUTES)


def _boot_times() -> tuple[dict[str, str], set[str]]:
    times: dict[str, str] = {}
    off: set[str] = set()
    for job in DAILY_JOBS.values():
        raw = os.environ.get(job.env_utc)
        if raw is not None and job.can_be_off and raw.strip().lower() in _OFF:
            off.add(job.key)
        from_env = parse_hhmm(raw) if raw is not None else None
        if raw is not None and from_env is None and job.key not in off:
            logger.warning("job_time_invalid", job=job.key, variable=job.env_utc, value=raw, using=job.default_ist)
        times[job.key] = utc_to_ist(from_env) if from_env else job.default_ist
    return times, off


# India time, HH:MM, per job -- and the jobs the environment switched off.
_times, _off = _boot_times()


def time_ist(job_key: str) -> str:
    return _times[job_key]


def time_utc(job_key: str) -> str:
    """What the worker's own clock reads: ``HH:MM`` in UTC, or ``off``."""
    return "off" if job_key in _off else ist_to_utc(_times[job_key])


def is_off(job_key: str) -> bool:
    return job_key in _off


def in_sleep_window(hhmm_ist: str) -> bool:
    """Whether a job at this time would wake the system from its night's sleep."""
    if sleep_window.WINDOW is None:
        return False
    start, end = sleep_window.WINDOW
    hour, _, minute = hhmm_ist.partition(":")
    at = clock_time(int(hour), int(minute))
    return start <= at < end if start < end else (at >= start or at < end)


def apply(times_ist: dict[str, str]) -> None:
    """Take these times and hand them to the code that runs the jobs, in this process."""
    for key, value in times_ist.items():
        hhmm = parse_hhmm(value)
        if key in DAILY_JOBS and hhmm:
            _times[key] = hhmm
    runtime = sys.modules.get("audit_core.uc03_p2_runtime")
    if runtime is not None:
        runtime.DAYEND_SWEEP_UTC = time_utc("DAYEND_CLEANUP")
    worker = sys.modules.get("audit_core.uc03_p2_worker")
    if worker is not None:
        worker._NIGHTLY_REVIEW_UTC = time_utc("NIGHTLY_REVIEW")
        worker._DATA_ARCHIVE_UTC = time_utc("DATA_ARCHIVE")


def stored_times(connection: Connection) -> dict[str, dict[str, Any]]:
    """The rows a SuperAdmin has set, by job."""
    return {
        row["job_key"]: dict(row)
        for row in connection.execute(
            text("SELECT job_key, run_time_ist, updated_by, updated_at_utc FROM platform_ops.job_schedules")
        ).mappings().all()
    }


def refresh(engine: Engine) -> None:
    """Read the table and follow it. Never while asleep: the times in memory stand."""
    if sleep_window.asleep():
        return
    with engine.begin() as connection:
        rows = stored_times(connection)
    apply({key: parse_hhmm(row["run_time_ist"]) or "" for key, row in rows.items()})


def set_time(connection: Connection, *, job_key: str, time_ist_value: str, changed_by: str | None) -> str:
    """A SuperAdmin's change: stored, logged, and followed by this process at once."""
    job = DAILY_JOBS[job_key]
    hhmm = parse_hhmm(time_ist_value)
    if hhmm is None:
        raise ValueError("not a time")
    old = connection.execute(
        text("SELECT run_time_ist FROM platform_ops.job_schedules WHERE job_key=:k"), {"k": job.key},
    ).scalar_one_or_none()
    connection.execute(
        text(
            """
            INSERT INTO platform_ops.job_schedules (job_key, run_time_ist, updated_by)
            VALUES (:k, CAST(:t AS time), :by)
            ON CONFLICT (job_key) DO UPDATE
              SET run_time_ist=excluded.run_time_ist, updated_by=excluded.updated_by, updated_at_utc=now()
            """
        ),
        {"k": job.key, "t": hhmm, "by": changed_by},
    )
    log_change(connection, job_key=job.key, old=parse_hhmm(old) or _times[job.key], new=hhmm, changed_by=changed_by)
    apply({job.key: hhmm})
    return hhmm


def log_change(connection: Connection, *, job_key: str, old: str | None, new: str, changed_by: str | None) -> None:
    connection.execute(
        text(
            """
            INSERT INTO platform_ops.job_schedule_changes (job_key, old_time_ist, new_time_ist, changed_by)
            VALUES (:k, CAST(:old AS time), CAST(:new AS time), :by)
            """
        ),
        {"k": job_key, "old": old, "new": new, "by": changed_by},
    )
