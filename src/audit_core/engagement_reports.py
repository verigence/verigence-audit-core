"""Employee engagement on Verigence: per person of one Project, by day, week or
month (product owner, 2026-10-27). Executive and Superadmin only; the route
checks the caller.

Every figure is read from what the system already holds, computed on read:
time on Verigence and sessions from the browser's own tally
(``p2_user_activity_daily``), output from uploads and journeys, work from task
events. Nothing here is a judgement; the efficacy figures are plain ratios so
people can be compared on the same footing.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from statistics import median
from typing import Any

from sqlalchemy import Connection, text

from audit_core.task_manager import (
    EXECUTIVE,
    TaskManagerError,
    known_names,
    project_people,
)
from audit_core.uc03_p2_object_keys import IST

MAX_DAYS = 93
PERIODS = ("day", "week", "month")
IDLE_THRESHOLD_MINUTES = 4
SESSION_GAP_MINUTES = 30

# Work on a Journey task (as in the Daily Processing report).
_WORK_EVENTS = (
    "UPLOAD_DOCUMENT", "REUPLOAD_DOCUMENT", "REVIEW_DOCUMENT", "CORRECT_EXTRACTED_FIELD", "ADD_EVIDENCE",
    "ADD_COMMENT", "PROVIDE_FEEDBACK", "REQUEST_CLARIFICATION", "COMPLETE_ACTION", "APPROVE_CORRECTION",
    "REJECT_CORRECTION", "ACCEPT", "REJECT", "ACCEPT_EXCEPTION", "PROVIDE_VEHICLE_ID", "CONFIRM_BREACH",
    "MARK_FALSE_POSITIVE", "CLOSE_TASK", "COMPLETED", "VERDICT",
)
_ROLE_ORDER = {EXECUTIVE: 0, "PM": 1, "TL": 2, "CRM": 3, "REVIEWER": 4, "PC": 5}


def period_key(day: date, period: str) -> date:
    if period == "week":
        return day - timedelta(days=day.isoweekday() - 1)
    if period == "month":
        return day.replace(day=1)
    return day


def period_label(start: date, period: str) -> str:
    if period == "week":
        end = start + timedelta(days=6)
        return f"{start:%d %b} – {end:%d %b %Y}"
    if period == "month":
        return f"{start:%B %Y}"
    return f"{start:%a %d %b %Y}"


def working_days(start: date, end: date) -> int:
    """Monday to Saturday (no daily task on Sunday, product owner 2026-10-26)."""
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).isoweekday() != 7)


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return (datetime.combine(start, time(0), tzinfo=IST).astimezone(UTC),
            datetime.combine(end + timedelta(days=1), time(0), tzinfo=IST).astimezone(UTC))


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _empty() -> dict[str, Any]:
    return {
        "activeMinutes": 0, "idleMinutes": 0, "backgroundMinutes": 0,
        # Active minutes by where they were spent: WEB (computer browser),
        # MOBILE_WEB (phone / tablet browser), ANDROID, IOS (the app).
        "activeMinutesBySource": {},
        "sessions": 0, "_sessionMinutes": [], "daysActive": 0,
        "firstActiveAtUtc": None, "lastActiveAtUtc": None, "_firstClock": [], "_lastClock": [],
        "hourlyActiveMinutes": [0] * 24,
        "journeysStarted": 0, "filesUploaded": 0, "pagesUploaded": 0, "badScans": 0, "notReadInTime": 0,
        "journeyTasksWorked": 0, "taskActions": 0,
        "tasksAssigned": 0, "tasksCompleted": 0, "tasksCompletedOnTime": 0, "sendBacksReceived": 0,
        "reviewsDone": 0,
    }


def _merge(into: dict[str, Any], part: dict[str, Any]) -> None:
    for key, value in part.items():
        if key == "hourlyActiveMinutes":
            into[key] = [a + b for a, b in zip(into[key], value, strict=True)]
        elif key == "activeMinutesBySource":
            for source, minutes in value.items():
                into[key][source] = into[key].get(source, 0) + minutes
        elif key in ("_sessionMinutes", "_firstClock", "_lastClock"):
            into[key] += value
        elif key == "firstActiveAtUtc":
            if value and (into[key] is None or value < into[key]):
                into[key] = value
        elif key == "lastActiveAtUtc":
            if value and (into[key] is None or value > into[key]):
                into[key] = value
        else:
            into[key] += value


def _clock(at: datetime) -> float:
    local = at.astimezone(IST)
    return local.hour * 60 + local.minute


def _clock_text(minutes: float | None) -> str | None:
    if minutes is None:
        return None
    m = round(minutes)
    return f"{m // 60:02d}:{m % 60:02d}"


def _finish(cell: dict[str, Any], *, work_days: int) -> dict[str, Any]:
    out = {k: v for k, v in cell.items() if not k.startswith("_")}
    sessions = cell["_sessionMinutes"]
    out["sessionMinutes"] = {
        "min": round(min(sessions)) if sessions else None,
        "avg": round(sum(sessions) / len(sessions)) if sessions else None,
        "max": round(max(sessions)) if sessions else None,
    }
    out["usualStart"] = _clock_text(median(cell["_firstClock"])) if cell["_firstClock"] else None
    out["usualEnd"] = _clock_text(median(cell["_lastClock"])) if cell["_lastClock"] else None
    out["firstActiveAtUtc"] = _iso(cell["firstActiveAtUtc"])
    out["lastActiveAtUtc"] = _iso(cell["lastActiveAtUtc"])
    opened = cell["activeMinutes"] + cell["idleMinutes"]
    active_hours = cell["activeMinutes"] / 60
    out["workingDays"] = work_days
    out["efficacy"] = {
        # Share of the time Verigence was open that it was actually being used.
        "inUsePct": round(100 * cell["activeMinutes"] / opened) if opened else None,
        "avgActiveMinutesPerDayActive": round(cell["activeMinutes"] / cell["daysActive"]) if cell["daysActive"] else None,
        "daysActivePct": round(100 * cell["daysActive"] / work_days) if work_days else None,
        "filesPerActiveHour": round(cell["filesUploaded"] / active_hours, 1) if active_hours >= 0.5 else None,
        "tasksPerActiveHour": (round((cell["journeyTasksWorked"] + cell["tasksCompleted"]) / active_hours, 1)
                               if active_hours >= 0.5 else None),
        "onTimePct": (round(100 * cell["tasksCompletedOnTime"] / cell["tasksCompleted"])
                      if cell["tasksCompleted"] else None),
        "badScanPct": round(100 * cell["badScans"] / cell["pagesUploaded"], 1) if cell["pagesUploaded"] else None,
    }
    return out


def build_engagement(connection: Connection, *, tenant_id: str, start_day: date, end_day: date,
                     period: str = "day", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if period not in PERIODS:
        raise TaskManagerError("Choose day, week or month.")
    if end_day < start_day:
        raise TaskManagerError("The end date is before the start date.")
    if (end_day - start_day).days + 1 > MAX_DAYS:
        raise TaskManagerError(f"Choose at most {MAX_DAYS} days.")
    start, end = _bounds(start_day, end_day)
    params = {"t": tenant_id, "s": start, "e": end}
    cells: dict[str, dict[date, dict[str, Any]]] = defaultdict(dict)

    def cell(actor: Any, day: date) -> dict[str, Any]:
        return cells[str(actor)].setdefault(day, _empty())

    day_of = "((x.at AT TIME ZONE 'Asia/Kolkata')::date)"

    def add(sql: str, key: str, value: str = "1") -> None:
        for row in connection.execute(text(f"""
            SELECT x.actor, {day_of} AS day, SUM({value}) AS n FROM ({sql}) x
            WHERE x.actor IS NOT NULL GROUP BY 1, 2
        """), params).mappings().all():
            cell(row["actor"], row["day"])[key] += int(row["n"] or 0)

    add("""SELECT created_by_actor_id AS actor, created_at_utc AS at FROM auditcore.journeys
           WHERE tenant_id = :t AND created_at_utc >= :s AND created_at_utc < :e""", "journeysStarted")
    add("""SELECT uploaded_by_actor_id AS actor, created_at_utc AS at, page_count FROM auditcore.p2_upload_batches
           WHERE tenant_id = :t AND created_at_utc >= :s AND created_at_utc < :e
             AND NOT (batch_status = 'CANCELLED' AND COALESCE(page_count, 0) = 0)""", "filesUploaded")
    add("""SELECT uploaded_by_actor_id AS actor, created_at_utc AS at, page_count FROM auditcore.p2_upload_batches
           WHERE tenant_id = :t AND created_at_utc >= :s AND created_at_utc < :e""", "pagesUploaded",
        "COALESCE(x.page_count, 0)")
    add("""SELECT b.uploaded_by_actor_id AS actor, b.created_at_utc AS at
           FROM auditcore.p2_document_queue q
           JOIN auditcore.p2_upload_batches b ON b.tenant_id = q.tenant_id AND b.batch_id = q.batch_id
           WHERE q.tenant_id = :t AND b.created_at_utc >= :s AND b.created_at_utc < :e
             AND q.last_error LIKE 'DI_QUALITY_%'""", "badScans")
    add("""SELECT b.uploaded_by_actor_id AS actor, b.created_at_utc AS at
           FROM auditcore.p2_document_queue q
           JOIN auditcore.p2_upload_batches b ON b.tenant_id = q.tenant_id AND b.batch_id = q.batch_id
           WHERE q.tenant_id = :t AND b.created_at_utc >= :s AND b.created_at_utc < :e
             AND q.last_error = 'NOT_READ_IN_30_MIN'""", "notReadInTime")
    add("""SELECT assigned_actor_id AS actor, created_at_utc AS at FROM auditcore.p2_tasks
           WHERE tenant_id = :t AND scope_kind <> 'JOURNEY' AND created_at_utc >= :s AND created_at_utc < :e""",
        "tasksAssigned")
    add("""SELECT completed_by_actor_id AS actor, action_completed_at_utc AS at FROM auditcore.p2_tasks
           WHERE tenant_id = :t AND scope_kind <> 'JOURNEY'
             AND action_completed_at_utc >= :s AND action_completed_at_utc < :e""", "tasksCompleted")
    add("""SELECT completed_by_actor_id AS actor, action_completed_at_utc AS at FROM auditcore.p2_tasks
           WHERE tenant_id = :t AND scope_kind <> 'JOURNEY'
             AND action_completed_at_utc >= :s AND action_completed_at_utc < :e
             AND (due_at_utc IS NULL OR action_completed_at_utc <= due_at_utc)""", "tasksCompletedOnTime")
    add("""SELECT tk.assigned_actor_id AS actor, te.created_at_utc AS at
           FROM auditcore.p2_task_events te
           JOIN auditcore.p2_tasks tk ON tk.tenant_id = te.tenant_id AND tk.task_id = te.task_id
           WHERE te.tenant_id = :t AND tk.scope_kind <> 'JOURNEY' AND te.event_type = 'REJECT'
             AND te.created_at_utc >= :s AND te.created_at_utc < :e""", "sendBacksReceived")
    add("""SELECT te.actor_id AS actor, te.created_at_utc AS at
           FROM auditcore.p2_task_events te
           JOIN auditcore.p2_tasks tk ON tk.tenant_id = te.tenant_id AND tk.task_id = te.task_id
           WHERE te.tenant_id = :t AND tk.scope_kind <> 'JOURNEY' AND te.event_type IN ('ACCEPT', 'REJECT')
             AND te.created_at_utc >= :s AND te.created_at_utc < :e""", "reviewsDone")
    for row in connection.execute(text("""
        SELECT te.actor_id AS actor, ((te.created_at_utc AT TIME ZONE 'Asia/Kolkata')::date) AS day,
               COUNT(DISTINCT te.task_id) AS tasks, COUNT(*) AS actions
        FROM auditcore.p2_task_events te
        WHERE te.tenant_id = :t AND te.journey_id IS NOT NULL AND te.created_at_utc >= :s AND te.created_at_utc < :e
          AND te.actor_id IS NOT NULL AND upper(te.actor_id) <> 'SYSTEM' AND te.event_type = ANY(:work)
        GROUP BY 1, 2
    """), {**params, "work": list(_WORK_EVENTS)}).mappings().all():
        c = cell(row["actor"], row["day"])
        c["journeyTasksWorked"] += int(row["tasks"])
        c["taskActions"] += int(row["actions"])

    for row in connection.execute(text("""
        SELECT actor_id, activity_date, client_kind, active_seconds, idle_seconds, background_seconds,
               hourly_active_minutes, first_active_at_utc, last_active_at_utc, sessions
        FROM auditcore.p2_user_activity_daily
        WHERE tenant_id = :t AND activity_date BETWEEN :sd AND :ed
    """), {"t": tenant_id, "sd": start_day, "ed": end_day}).mappings().all():
        c = cell(row["actor_id"], row["activity_date"])
        c["activeMinutes"] += round(int(row["active_seconds"]) / 60)
        source = str(row["client_kind"] or "WEB")
        c["activeMinutesBySource"][source] = (c["activeMinutesBySource"].get(source, 0)
                                              + round(int(row["active_seconds"]) / 60))
        c["idleMinutes"] += round(int(row["idle_seconds"]) / 60)
        c["backgroundMinutes"] += round(int(row["background_seconds"]) / 60)
        hourly = [int(v or 0) for v in list(row["hourly_active_minutes"] or [])] + [0] * 24
        c["hourlyActiveMinutes"] = [min(60, a + b) for a, b in zip(c["hourlyActiveMinutes"], hourly[:24], strict=True)]
        for key, column, pick in (("firstActiveAtUtc", "first_active_at_utc", min),
                                  ("lastActiveAtUtc", "last_active_at_utc", max)):
            if row[column] is not None:
                c[key] = row[column] if c[key] is None else pick(c[key], row[column])
        for session in row["sessions"] or []:
            try:
                s_at = datetime.fromisoformat(str(session["start"]))
                e_at = datetime.fromisoformat(str(session["end"]))
            except (KeyError, ValueError):
                continue
            c["sessions"] += 1
            c["_sessionMinutes"].append(max(0.0, (e_at - s_at).total_seconds() / 60))

    # A day counts as active once there is any interaction or any work.
    for per_day in cells.values():
        for c in per_day.values():
            first, last = c["firstActiveAtUtc"], c["lastActiveAtUtc"]
            if first is not None:
                c["_firstClock"].append(_clock(first))
            if last is not None:
                c["_lastClock"].append(_clock(last))
            did_work = (c["activeMinutes"] or c["filesUploaded"] or c["journeyTasksWorked"]
                        or c["tasksCompleted"] or c["journeysStarted"] or c["reviewsDone"])
            c["daysActive"] = 1 if did_work else 0

    span = [start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)]
    period_starts = sorted({period_key(d, period) for d in span})

    def period_days(p: date) -> tuple[date, date]:
        days = [d for d in span if period_key(d, period) == p]
        return days[0], days[-1]

    people = {p["actorId"]: p for p in project_people(connection, tenant_id=tenant_id)}
    names = known_names(connection, tenant_id=tenant_id)
    out_people = []
    for actor in sorted(set(people) | set(cells)):
        if actor.upper() == "SYSTEM":
            continue
        per_day = cells.get(actor, {})
        total = _empty()
        series = []
        for p in period_starts:
            first_day, last_day = period_days(p)
            agg = _empty()
            for d in span:
                if first_day <= d <= last_day and d in per_day:
                    _merge(agg, per_day[d])
            _merge(total, agg)
            series.append({"periodStart": p.isoformat(), "label": period_label(p, period),
                           "from": first_day.isoformat(), "to": last_day.isoformat(),
                           **_finish(agg, work_days=working_days(first_day, last_day))})
        person = people.get(actor)
        out_people.append({
            "actorId": actor, "name": (person or {}).get("displayName") or names.get(actor),
            "role": (person or {}).get("roleCode"), "activeInProject": person is not None,
            "outlets": [o["outletName"] for o in (person or {}).get("outlets", []) if o.get("outletName")],
            "totals": _finish(total, work_days=working_days(start_day, end_day)),
            "periods": series,
        })
    out_people.sort(key=lambda p: (_ROLE_ORDER.get(p["role"] or "", 9), (p["name"] or p["actorId"]).lower()))
    return {
        "from": start_day.isoformat(), "to": end_day.isoformat(), "period": period,
        "periods": [{"periodStart": p.isoformat(), "label": period_label(p, period)} for p in period_starts],
        "workingDays": working_days(start_day, end_day),
        "idleThresholdMinutes": IDLE_THRESHOLD_MINUTES, "sessionGapMinutes": SESSION_GAP_MINUTES,
        "people": out_people,
        "notes": [
            ("Time on Verigence is measured in the browser: active = a click, tap, key or scroll with the app "
             f"visible; idle = visible with no interaction for {IDLE_THRESHOLD_MINUTES} minutes or more; "
             "background = tab hidden or app minimised."),
            (f"A session is a stretch of use with no break of {SESSION_GAP_MINUTES} minutes or more. Sessions are "
             "counted from 27 Oct 2026; days before show time but no sessions."),
            ("Efficacy figures are plain ratios: files and tasks per active hour, tasks closed on time, the share "
             "of uploaded pages refused as bad scans, days active out of working days (Monday to Saturday)."),
            ("Time measures interaction, not work: read it next to the output. Minutes since the last send are "
             "lost if the browser is closed abruptly (an undercount, never an overcount)."),
        ],
    }
