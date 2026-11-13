"""SuperAdmin "Jobs" page: every job the platform runs on a clock, and the time of the daily ones.

Product owner, 13 Nov 2026: "create dashboard of all jobs which get executed for
Superadmin and allow him to change the time".

GET /v1/admin/jobs lists, for the whole platform:
- ``daily``: the jobs that run once a day for every live Project (day-end clean-up, nightly
  review, data archive): the time now, the default, who changed it and when, when it last ran
  and for how many Projects, and whether the time falls in the night's sleep;
- ``driveRuns``: each Project's daily Google Drive run (its time belongs to the Project);
- ``recurring``: what runs every few seconds or minutes while the system is awake;
- ``external``: jobs other modules run on their own clocks, shown so the list is whole;
- ``sleepWindow`` and the last ``changes``.

PUT /v1/admin/jobs/{jobKey} sets a daily job's time (India time, HH:MM).
PUT /v1/admin/jobs/drive-runs/{tenantId} sets a Project's Drive run time.
Both are logged (platform_ops.job_schedule_changes) and followed within a minute.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core import job_schedule, sleep_window
from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)

router = APIRouter(tags=["jobs"])

DRIVE_RUN_NAME = "P2_DRIVE_END_OF_DAY"
DRIVE_DEFAULT_IST = "21:00"

_WORKER = "P2 worker"
_API = "Audit Core API"

# What runs on a short clock. Fixed in the code; listed so the page shows everything.
RECURRING: list[dict[str, Any]] = [
    {"name": "Document work queue", "every": "1 second", "service": _WORKER,
     "what": "Picks up the next pages and checks to work on."},
    {"name": "Facts and price variance sweep", "every": "30 seconds", "service": _WORKER,
     "what": "Brings a journey's stored facts and price variance up to date where a change was missed."},
    {"name": "Upload watchdog", "every": "1 minute", "service": _WORKER,
     "what": "Accepts files stored whole that the browser never confirmed, and fails a page not read within its time limit."},
    {"name": "Task Manager and closure auto-accept", "every": "5 minutes", "service": _WORKER,
     "what": "Raises the recurring tasks that are due and accepts closures whose waiting time is over."},
    {"name": "Journey exports", "every": "15 seconds", "service": _WORKER,
     "what": "Builds the Excel exports Team Leads have asked for."},
    {"name": "Jobs requested from Administration", "every": "1 minute", "service": _WORKER,
     "what": "Starts a one-off job a SuperAdmin asked for (SKU run, re-reads and the like)."},
    {"name": "Corrections after a re-read", "every": "3 minutes", "service": _WORKER,
     "what": "Carries a person's corrections over to a page that was read again."},
    {"name": "Cost-sheet statements", "every": "1 hour", "service": _WORKER,
     "what": "Reads a dealer's statement filed as a cost sheet again as the NDC."},
    {"name": "Drive clock", "every": "5 minutes", "service": _WORKER,
     "what": "Starts each Project's Drive run at its time and brings back what the reading decided."},
    {"name": "People's names and leavers", "every": "6 hours", "service": _WORKER,
     "what": "Refreshes names from Security and ends the roles of people whose last day is over."},
    {"name": "Stale task recovery", "every": "5 minutes", "service": _API,
     "what": "Frees a task a stopped process left locked."},
    {"name": "Document sync recovery", "every": "5 minutes", "service": _API,
     "what": "Copies again a document whose copy from Document Intelligence did not finish."},
    {"name": "Heartbeat", "every": "1 minute", "service": f"{_API}, {_WORKER}",
     "what": "Records that the service is running, with its CPU, memory and request counts (System Health)."},
]
for _job in RECURRING:
    _job["pausedAsleep"] = True

# Jobs other modules run. Not set here; shown so nothing that runs on a clock is missing.
EXTERNAL: list[dict[str, Any]] = [
    {"name": "Analytics copy", "service": "Analytics (GitHub Actions)", "when": "Daily, 08:30 IST",
     "what": "Copies the previous day's data into the Analytics tables.",
     "note": "Set in the verigence-analytics repository. In the morning, after the night's sleep (13 Nov 2026)."},
    {"name": "End-of-day retry", "service": "Document Intelligence", "when": "Daily, each Project's own time",
     "what": "Gives every document that failed during the day one more reading.",
     "note": "Set per Project in Document Intelligence."},
    {"name": "Heartbeat", "service": "Security", "when": "Every minute",
     "what": "Records that Security is running (System Health).",
     "note": "Not yet paused during the night's sleep."},
    {"name": "Project sync", "service": "SevaSutra", "when": "Every 5 minutes",
     "what": "Brings SevaSutra's dealerships, outlets and role mappings in step with Project Administration.",
     "note": "Not yet paused during the night's sleep."},
]


class JobTimeCommand(BaseModel):
    timeIst: str = Field(min_length=4, max_length=8)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _hhmm(window_time: Any) -> str:
    return f"{window_time.hour:02d}:{window_time.minute:02d}"


def _projects(connection: Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT tenant_id, min(project_name) AS project_name
            FROM auditcore.projects WHERE project_status='ACTIVE'
            GROUP BY tenant_id ORDER BY 2
            """
        )
    ).mappings().all()]


def _time_or_422(value: str) -> str:
    hhmm = job_schedule.parse_hhmm(value)
    if hhmm is None:
        raise HTTPException(status_code=422, detail="Give the time as HH:MM on a 24-hour clock, India time (21:30).")
    return hhmm


def _overview(connection: Connection) -> dict[str, Any]:
    set_platform_super_admin_context(connection)
    stored = job_schedule.stored_times(connection)
    job_schedule.apply({key: job_schedule.parse_hhmm(row["run_time_ist"]) or "" for key, row in stored.items()})
    changes = [dict(r) for r in connection.execute(
        text(
            """
            SELECT job_key, old_time_ist, new_time_ist, changed_by, changed_at_utc
            FROM platform_ops.job_schedule_changes ORDER BY changed_at_utc DESC, change_id DESC LIMIT 20
            """
        )
    ).mappings().all()]
    projects = _projects(connection)
    run_names = [job.run_name for job in job_schedule.DAILY_JOBS.values()] + [DRIVE_RUN_NAME]
    last: dict[str, list[dict[str, Any]]] = {name: [] for name in run_names}
    drive_runs: list[dict[str, Any]] = []
    for project in projects:
        tenant_id = project["tenant_id"]
        set_tenant_context(connection, tenant_id)
        for row in connection.execute(
            text(
                """
                SELECT run_name, max(claimed_at_utc) AS last_at
                FROM auditcore.p2_scheduled_runs
                WHERE tenant_id=:t AND run_name = ANY(:names) GROUP BY run_name
                """
            ),
            {"t": tenant_id, "names": run_names},
        ).mappings().all():
            last[row["run_name"]].append({"tenant_id": tenant_id, "last_at": row["last_at"]})
        link = connection.execute(
            text("SELECT daily_run_time, daily_run_enabled FROM auditcore.p2_drive_connections WHERE tenant_id=:t"),
            {"t": tenant_id},
        ).mappings().one_or_none()
        if link is not None:
            time_ist = job_schedule.parse_hhmm(link["daily_run_time"]) or DRIVE_DEFAULT_IST
            ran = next((r["last_at"] for r in last[DRIVE_RUN_NAME] if r["tenant_id"] == tenant_id), None)
            drive_runs.append({
                "tenantId": tenant_id, "projectName": project["project_name"], "timeIst": time_ist,
                "enabled": bool(link["daily_run_enabled"]), "lastRunAtUtc": _iso(ran),
                "insideSleepWindow": job_schedule.in_sleep_window(time_ist),
            })

    names = {p["tenant_id"]: p["project_name"] for p in projects}
    daily = []
    for job in job_schedule.DAILY_JOBS.values():
        runs = last[job.run_name]
        latest = max((r["last_at"] for r in runs), default=None)
        row = stored.get(job.key)
        time_ist = job_schedule.time_ist(job.key)
        daily.append({
            "key": job.key, "name": job.name, "what": job.what, "service": _WORKER,
            "timeIst": time_ist, "defaultTimeIst": job.default_ist,
            "enabled": not job_schedule.is_off(job.key), "editable": True,
            "insideSleepWindow": job_schedule.in_sleep_window(time_ist),
            "changedBy": row["updated_by"] if row else None,
            "changedAtUtc": _iso(row["updated_at_utc"]) if row else None,
            "lastRunAtUtc": _iso(latest),
            # Projects whose latest run is within a day of the newest one: all of them on a good night.
            "projectsRunLast": sum(1 for r in runs if latest is not None
                                   and (latest - r["last_at"]).total_seconds() < 86400),
            "projects": len(projects),
        })

    def _label(job_key: str) -> str:
        if job_key in job_schedule.DAILY_JOBS:
            return job_schedule.DAILY_JOBS[job_key].name
        prefix, _, tenant_id = job_key.partition(":")
        return f"Drive run, {names.get(tenant_id, tenant_id)}" if prefix == "DRIVE_RUN" else job_key

    window = sleep_window.WINDOW
    return {
        "timezone": "Asia/Kolkata",
        "sleepWindow": {
            "enabled": window is not None,
            "startIst": _hhmm(window[0]) if window else None,
            "endIst": _hhmm(window[1]) if window else None,
        },
        "daily": daily,
        "driveRuns": drive_runs,
        "recurring": RECURRING,
        "external": EXTERNAL,
        "changes": [
            {"job": _label(c["job_key"]), "jobKey": c["job_key"],
             "oldTimeIst": job_schedule.parse_hhmm(c["old_time_ist"]),
             "newTimeIst": job_schedule.parse_hhmm(c["new_time_ist"]),
             "changedBy": c["changed_by"], "changedAtUtc": _iso(c["changed_at_utc"])}
            for c in changes
        ],
    }


@router.get("/v1/admin/jobs")
def list_jobs(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    return _overview(connection)


@router.put("/v1/admin/jobs/drive-runs/{tenant_id}")
def set_drive_run_time(
    tenant_id: str,
    command: JobTimeCommand,
    admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The Project's Drive run time: the same setting as on the Project's Drive page."""
    hhmm = _time_or_422(command.timeIst)
    set_platform_super_admin_context(connection)
    set_tenant_context(connection, tenant_id)
    link = connection.execute(
        text("SELECT daily_run_time FROM auditcore.p2_drive_connections WHERE tenant_id=:tenant FOR UPDATE"),
        {"tenant": tenant_id},
    ).mappings().one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="This Project's Google Drive is not connected.")
    old = link["daily_run_time"]
    connection.execute(
        text("UPDATE auditcore.p2_drive_connections SET daily_run_time=CAST(:t AS time) WHERE tenant_id=:tenant"),
        {"t": hhmm, "tenant": tenant_id},
    )
    job_schedule.log_change(connection, job_key=f"DRIVE_RUN:{tenant_id}", old=job_schedule.parse_hhmm(old),
                            new=hhmm, changed_by=admin.user_id)
    return _overview(connection)


@router.put("/v1/admin/jobs/{job_key}")
def set_job_time(
    job_key: str,
    command: JobTimeCommand,
    admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    if job_key not in job_schedule.DAILY_JOBS:
        raise HTTPException(status_code=404, detail="There is no daily job of this name.")
    hhmm = _time_or_422(command.timeIst)
    set_platform_super_admin_context(connection)
    job_schedule.set_time(connection, job_key=job_key, time_ist_value=hhmm, changed_by=admin.user_id)
    return _overview(connection)
