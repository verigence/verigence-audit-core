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
from itertools import pairwise
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

    minute_rows: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for row in connection.execute(text("""
        SELECT actor_id, activity_date, client_kind, active_seconds, idle_seconds, background_seconds,
               hourly_active_minutes, first_active_at_utc, last_active_at_utc, sessions, minute_states
        FROM auditcore.p2_user_activity_daily
        WHERE tenant_id = :t AND activity_date BETWEEN :sd AND :ed
    """), {"t": tenant_id, "sd": start_day, "ed": end_day}).mappings().all():
        c = cell(row["actor_id"], row["activity_date"])
        if row["minute_states"]:
            minute_rows[(str(row["actor_id"]), row["activity_date"])].append(dict(row))
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
    # Each day with minute detail (from 28 Oct 2026): the longest stretch with
    # no output and the 20-minute blocks (product owner 2026-10-28).
    by_day: dict[tuple[str, date], list[int]] = defaultdict(list)
    if minute_rows:
        for o in outputs(connection, tenant_id=tenant_id, start=start, end=end):
            role = (people.get(o["actor"]) or {}).get("roleCode")
            if counts_as_output(o["kind"], role):
                d = o["at"].astimezone(IST).date()
                by_day[(o["actor"], d)].append(_minute_of(o["at"], d))
    day_detail = {key: day_analysis(merged_minutes(rows), by_day.get(key, [])) for key, rows in minute_rows.items()}
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
            # Each day with time on Verigence: when the person started and
            # stopped, for "who was in first / out last" (product owner 2026-10-27).
            "days": [{"day": d.isoformat(), "activeMinutes": per_day[d]["activeMinutes"],
                      "firstActiveAtUtc": _iso(per_day[d]["firstActiveAtUtc"]),
                      "lastActiveAtUtc": _iso(per_day[d]["lastActiveAtUtc"]),
                      **_day_summary(day_detail.get((actor, d)))}
                     for d in span if d in per_day and per_day[d]["activeMinutes"]],
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


def _day_summary(analysis: dict[str, Any] | None) -> dict[str, Any]:
    if not analysis:
        return {}
    longest = analysis.get("longestNoOutput") or {}
    return {"longestNoOutputMinutes": longest.get("minutes"), "longestNoOutputFrom": longest.get("from"),
            "longestNoOutputTo": longest.get("to"), "blockCounts": analysis.get("blockCounts")}


# --------------------------------------------------------------------------- a person's day, minute by minute
# Product owner 2026-10-28: "a user cannot be active for 4 hours without doing
# any updates". Which screens were used, the longest stretch with no output,
# and the day in 20-minute blocks -- Superadmin and Executive only.

BLOCK_MINUTES = 20
SCROLL_ONLY_MINUTES = 10
# What counts as output. A PC's output is what they save; a Reviewer's (and a
# supervisor's) also includes opening a document, since reviewing is reading
# (product owner 2026-10-28: "for reviewer journeys, opening documents,
# corrections, everything should be counted").
_OPENING_COUNTS = frozenset({"REVIEWER", "TL", "PM", "CRM", EXECUTIVE})
_PAGE_EVENTS = ("PAGE_REMOVED", "PAGE_RETYPED", "PAGE_RETRY_REQUESTED", "PAGE_REREAD_REQUESTED",
                "PAGE_SENT_AGAIN", "DOCUMENT_COPY_RESTORED")
OUTPUT_LABELS = {
    "FILE_UPLOADED": "File uploaded", "JOURNEY_STARTED": "Journey started", "VALUE_CORRECTED": "Value corrected",
    "VALUE_CONFIRMED": "Value confirmed", "DOCUMENT_OPENED": "Document opened",
    "DOCUMENTS_MARKED_REVIEWED": "Documents marked reviewed", "PAGE_REMOVED": "Page removed",
    "PAGE_RETYPED": "Document type set", "PAGE_RETRY_REQUESTED": "Page retried", "PAGE_REREAD_REQUESTED": "Page re-read",
    "PAGE_SENT_AGAIN": "Page sent again", "DOCUMENT_COPY_RESTORED": "Document copy restored",
}


def outputs(connection: Connection, *, tenant_id: str, start: datetime, end: datetime,
            actor_id: str | None = None) -> list[dict[str, Any]]:
    """Everything a person saved (or, for a Reviewer, opened) between start
    and end: {actor, at, kind, journeyId, detail}."""
    params = {"t": tenant_id, "s": start, "e": end, "a": actor_id, "work": list(_WORK_EVENTS),
              "pages": list(_PAGE_EVENTS)}
    who = "(CAST(:a AS text) IS NULL OR {col} = CAST(:a AS text))"
    rows = connection.execute(text(f"""
        SELECT uploaded_by_actor_id::text AS actor, created_at_utc AS at, 'FILE_UPLOADED' AS kind,
               journey_id, original_filename AS detail
          FROM auditcore.p2_upload_batches
         WHERE tenant_id = :t AND created_at_utc >= :s AND created_at_utc < :e
           AND {who.format(col='uploaded_by_actor_id::text')}
        UNION ALL
        SELECT created_by_actor_id::text, created_at_utc, 'JOURNEY_STARTED', journey_id, journey_reference
          FROM auditcore.journeys
         WHERE tenant_id = :t AND created_at_utc >= :s AND created_at_utc < :e
           AND {who.format(col='created_by_actor_id::text')}
        UNION ALL
        SELECT te.actor_id, te.created_at_utc, 'TASK_' || te.event_type, te.journey_id, tk.title
          FROM auditcore.p2_task_events te
          LEFT JOIN auditcore.p2_tasks tk ON tk.tenant_id = te.tenant_id AND tk.task_id = te.task_id
         WHERE te.tenant_id = :t AND te.created_at_utc >= :s AND te.created_at_utc < :e
           AND te.event_type = ANY(:work) AND upper(COALESCE(te.actor_id, '')) <> 'SYSTEM'
           AND {who.format(col='te.actor_id')}
        UNION ALL
        SELECT modified_by_actor_id::text, modified_at_utc, 'VALUE_CORRECTED', journey_id, field_key
          FROM auditcore.journey_document_extracted_fields
         WHERE tenant_id = :t AND modified_at_utc >= :s AND modified_at_utc < :e
           AND {who.format(col='modified_by_actor_id::text')}
        UNION ALL
        SELECT reviewed_by_actor_id::text, reviewed_at_utc, 'VALUE_CONFIRMED', journey_id, field_key
          FROM auditcore.journey_document_extracted_fields
         WHERE tenant_id = :t AND reviewed_at_utc >= :s AND reviewed_at_utc < :e
           AND (modified_at_utc IS NULL OR modified_at_utc <> reviewed_at_utc)
           AND {who.format(col='reviewed_by_actor_id::text')}
        UNION ALL
        SELECT reviewed_by_actor_id::text, reviewed_at_utc, 'DOCUMENTS_MARKED_REVIEWED', journey_id, stage_code
          FROM auditcore.p2_stage_document_reviews
         WHERE tenant_id = :t AND reviewed_at_utc >= :s AND reviewed_at_utc < :e
           AND {who.format(col='reviewed_by_actor_id::text')}
        UNION ALL
        SELECT actor_id, first_opened_at_utc, 'DOCUMENT_OPENED', journey_id, di_document_id::text
          FROM auditcore.p2_document_views
         WHERE tenant_id = :t AND first_opened_at_utc >= :s AND first_opened_at_utc < :e
           AND {who.format(col='actor_id')}
        UNION ALL
        SELECT actor_id, last_opened_at_utc, 'DOCUMENT_OPENED', journey_id, di_document_id::text
          FROM auditcore.p2_document_views
         WHERE tenant_id = :t AND last_opened_at_utc >= :s AND last_opened_at_utc < :e
           AND last_opened_at_utc > first_opened_at_utc + interval '1 minute'
           AND {who.format(col='actor_id')}
        UNION ALL
        SELECT COALESCE(details ->> 'removedBy', details ->> 'by'), created_at_utc, event_type, journey_id,
               details ->> 'filename'
          FROM auditcore.p2_activity_events
         WHERE tenant_id = :t AND created_at_utc >= :s AND created_at_utc < :e AND event_type = ANY(:pages)
           AND {who.format(col="COALESCE(details ->> 'removedBy', details ->> 'by')")}
    """), params).mappings().all()
    return [{"actor": str(r["actor"]), "at": r["at"], "kind": str(r["kind"]),
             "journeyId": str(r["journey_id"]) if r["journey_id"] else None, "detail": r["detail"]}
            for r in rows if r["actor"] and r["at"] is not None]


def output_label(kind: str) -> str:
    if kind.startswith("TASK_"):
        return "Task: " + kind[5:].replace("_", " ").lower()
    return OUTPUT_LABELS.get(kind, kind.replace("_", " ").capitalize())


def counts_as_output(kind: str, role: str | None) -> bool:
    return kind != "DOCUMENT_OPENED" or (role or "").upper() in _OPENING_COUNTS


def _minute_of(at: datetime, day: date) -> int:
    local = at.astimezone(IST)
    if local.date() != day:
        return -1
    return local.hour * 60 + local.minute


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def day_analysis(minute_states: str | None, output_minutes: list[int]) -> dict[str, Any] | None:
    """The day minute by minute: the longest stretch without output, the
    20-minute blocks, and stretches of scrolling with no click. None when the
    browser sent no minutes for the day."""
    if not minute_states:
        return None
    active = [i for i, c in enumerate(minute_states) if c in "AK"]
    used = [i for i, c in enumerate(minute_states) if c in "AKI"]
    if not active:
        return {"firstActive": None, "lastActive": None, "longestNoOutput": None, "blocks": [],
                "blockCounts": {"WORKING": 0, "ACTIVE_NO_OUTPUT": 0, "IDLE": 0, "AWAY": 0},
                "scrollOnly": [], "activeMinutes": 0}
    first, last = active[0], active[-1]
    marks = sorted({m for m in output_minutes if first <= m <= last})
    edges = [first, *marks, last + 1]
    longest = None
    for a, b in pairwise(edges):
        span = b - a
        if longest is None or span > longest["minutes"]:
            longest = {"minutes": span, "from": _hhmm(a), "to": _hhmm(min(b, 1439)),
                       "activeMinutes": sum(1 for i in range(a, b) if minute_states[i] in "AK"),
                       "endsWithOutput": b in marks}
    out_set = set(output_minutes)
    start_block = (min(used[0], first) // BLOCK_MINUTES) * BLOCK_MINUTES
    end_block = (max(used[-1], last) // BLOCK_MINUTES) * BLOCK_MINUTES
    blocks = []
    counts = {"WORKING": 0, "ACTIVE_NO_OUTPUT": 0, "IDLE": 0, "AWAY": 0}
    for b in range(start_block, end_block + BLOCK_MINUTES, BLOCK_MINUTES):
        window = minute_states[b:b + BLOCK_MINUTES]
        n_out = sum(1 for m in out_set if b <= m < b + BLOCK_MINUTES)
        n_active = sum(1 for c in window if c in "AK")
        n_idle = window.count("I")
        state = ("WORKING" if n_out else "ACTIVE_NO_OUTPUT" if n_active else "IDLE" if n_idle else "AWAY")
        counts[state] += 1
        blocks.append({"from": _hhmm(b), "to": _hhmm(b + BLOCK_MINUTES) if b + BLOCK_MINUTES < 1440 else "24:00",
                       "state": state, "outputs": n_out, "activeMinutes": n_active,
                       "idleMinutes": n_idle, "hiddenMinutes": window.count("B")})
    scroll_only = []
    run_start = None
    for i in range(first, last + 2):
        c = minute_states[i] if i <= 1439 else "."
        if c == "A":
            run_start = i if run_start is None else run_start
            continue
        if run_start is not None and i - run_start >= SCROLL_ONLY_MINUTES:
            scroll_only.append({"from": _hhmm(run_start), "to": _hhmm(min(i, 1439)), "minutes": i - run_start})
        run_start = None
    return {"firstActive": _hhmm(first), "lastActive": _hhmm(last), "longestNoOutput": longest,
            "blocks": blocks, "blockCounts": counts, "scrollOnly": scroll_only, "activeMinutes": len(active)}


def merged_minutes(rows: list[dict[str, Any]]) -> str | None:
    from audit_core.task_manager_reports import merge_minute_states

    merged: str | None = None
    for row in rows:
        merged = merge_minute_states(merged, row.get("minute_states"))
    return merged


def person_day(connection: Connection, *, tenant_id: str, actor_id: str, day: date) -> dict[str, Any]:
    """One person's day: screens used, output, the longest stretch with no
    output and the 20-minute blocks."""
    start, end = _bounds(day, day)
    people = {p["actorId"]: p for p in project_people(connection, tenant_id=tenant_id)}
    person = people.get(actor_id)
    role = (person or {}).get("roleCode")
    rows = [dict(r) for r in connection.execute(text("""
        SELECT client_kind, active_seconds, idle_seconds, background_seconds, minute_states, screen_visits,
               first_active_at_utc, last_active_at_utc
        FROM auditcore.p2_user_activity_daily
        WHERE tenant_id = :t AND actor_id = :a AND activity_date = :d
    """), {"t": tenant_id, "a": actor_id, "d": day}).mappings().all()]
    work = sorted(outputs(connection, tenant_id=tenant_id, start=start, end=end, actor_id=actor_id),
                  key=lambda o: o["at"])
    counted = [o for o in work if counts_as_output(o["kind"], role)]
    minutes = merged_minutes(rows)
    analysis = day_analysis(minutes, [_minute_of(o["at"], day) for o in counted])

    visits = sorted((v for r in rows for v in (r.get("screen_visits") or []) if isinstance(v, dict)),
                    key=lambda v: str(v.get("at")))
    last_active = max((r["last_active_at_utc"] for r in rows if r["last_active_at_utc"]), default=None)
    timeline = []
    per_screen: dict[str, int] = defaultdict(int)
    visit_minutes = []
    for i, v in enumerate(visits):
        at = datetime.fromisoformat(str(v["at"]))
        nxt = datetime.fromisoformat(str(visits[i + 1]["at"])) if i + 1 < len(visits) else (last_active or at)
        visit_minutes.append(_minute_of(at, day))
        timeline.append({"at": at.isoformat(), "time": at.astimezone(IST).strftime("%H:%M"), "screen": v["screen"],
                         "journeyId": v.get("journeyId"), "minutes": max(0, round((max(nxt, at) - at).total_seconds() / 60))})
    if minutes and visit_minutes:
        for m, c in enumerate(minutes):
            if c not in "AK":
                continue
            current = next((visits[j]["screen"] for j in range(len(visits) - 1, -1, -1)
                            if 0 <= visit_minutes[j] <= m), None)
            per_screen[current or "Before the first screen recorded"] += 1

    journey_ids = sorted({j for j in [*(o["journeyId"] for o in work), *(v.get("journeyId") for v in visits)] if j})
    refs = {str(r[0]): r[1] for r in connection.execute(
        text("SELECT journey_id, journey_reference FROM auditcore.journeys WHERE tenant_id = :t "
             "AND journey_id::text = ANY(:ids)"), {"t": tenant_id, "ids": journey_ids}).all()} if journey_ids else {}
    for item in timeline:
        item["journeyReference"] = refs.get(str(item["journeyId"])) if item["journeyId"] else None
    names = known_names(connection, tenant_id=tenant_id)
    return {
        "actorId": actor_id, "name": (person or {}).get("displayName") or names.get(actor_id), "role": role,
        "day": day.isoformat(),
        "activeMinutes": round(sum(int(r["active_seconds"]) for r in rows) / 60),
        "idleMinutes": round(sum(int(r["idle_seconds"]) for r in rows) / 60),
        "hiddenMinutes": round(sum(int(r["background_seconds"]) for r in rows) / 60),
        "minuteDetail": minutes is not None,
        "analysis": analysis,
        "outputs": [{"at": o["at"].isoformat(), "time": o["at"].astimezone(IST).strftime("%H:%M"), "kind": o["kind"],
                     "label": output_label(o["kind"]), "countsAsOutput": counts_as_output(o["kind"], role),
                     "journeyId": o["journeyId"], "journeyReference": refs.get(str(o["journeyId"])) if o["journeyId"] else None,
                     "detail": o["detail"]} for o in work],
        "outputCount": len(counted),
        "journeysReviewed": len({o["journeyId"] for o in work if o["kind"] == "DOCUMENTS_MARKED_REVIEWED"}),
        "documentsOpened": len({o["detail"] for o in work if o["kind"] == "DOCUMENT_OPENED"}),
        "journeysTouched": len(journey_ids),
        "screens": timeline,
        "minutesByScreen": [{"screen": k, "activeMinutes": v}
                            for k, v in sorted(per_screen.items(), key=lambda kv: -kv[1])],
        "notes": [
            ("Output for a PC: files uploaded, journeys started, task actions, values corrected or confirmed, pages "
             "removed, retried or retyped. For a Reviewer (and supervisors) opening a document also counts."),
            (f"The day is cut into {BLOCK_MINUTES}-minute blocks: Working (at least one output), Active with no "
             "output (clicks or scrolls, nothing saved), Idle (open, untouched), Away (hidden or closed)."),
            f"Scrolling only: {SCROLL_ONLY_MINUTES} minutes or more active with no click, tap or key.",
            "Minute-by-minute detail and screens are recorded from 28 Oct 2026; earlier days show totals only.",
        ],
    }
