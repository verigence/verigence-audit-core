"""Daily task status, employee utilisation and time on Verigence
(product owner, 2026-10-26; docs/TASK_MANAGER_DESIGN_v1.0.md sections 7-9).

All figures are per Project and computed on read from indexed ranges; nothing
here runs as a batch. Time on Verigence comes from the browser's own tally
(one row per person, Project, IST day and client), only ever raised.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Connection, text

from audit_core.task_manager import (
    EXECUTIVE,
    Caller,
    TaskManagerError,
    known_names,
    normalise_role,
    project_people,
)
from audit_core.uc03_p2_object_keys import IST

MAX_UTILISATION_DAYS = 31
IDLE_THRESHOLD_MINUTES = 4
_FLAG_LIMIT = 300

# A person's work on a Journey task (as in the Daily Processing report).
_WORK_EVENTS = (
    "UPLOAD_DOCUMENT", "REUPLOAD_DOCUMENT", "REVIEW_DOCUMENT", "CORRECT_EXTRACTED_FIELD", "ADD_EVIDENCE",
    "ADD_COMMENT", "PROVIDE_FEEDBACK", "REQUEST_CLARIFICATION", "COMPLETE_ACTION", "APPROVE_CORRECTION",
    "REJECT_CORRECTION", "ACCEPT", "REJECT", "ACCEPT_EXCEPTION", "PROVIDE_VEHICLE_ID", "CONFIRM_BREACH",
    "MARK_FALSE_POSITIVE", "CLOSE_TASK", "COMPLETED", "VERDICT",
)


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=IST).astimezone(UTC)
    return start, start + timedelta(days=1)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# --------------------------------------------------------------------------- daily status

_STATUS_SQL = """
    WITH tasks AS (
      SELECT t.task_id, t.scope_kind, t.journey_id, t.task_type, t.title, t.task_status, t.schedule_id,
             t.completion_protocol, t.due_at_utc, t.review_due_at_utc, t.created_at_utc, t.missed_at_utc,
             t.completion_result, t.reviewer_role_code, t.reviewer_actor_id,
             upper(t.assigned_role_code) AS assigned_role,
             COALESCE(t.assigned_actor_id,
                      CASE WHEN upper(t.assigned_role_code) = 'PC' THEN j.created_by_actor_id END) AS person,
             COALESCE(t.action_completed_at_utc,
                      CASE WHEN t.task_status = 'VERIFIED_COMPLETE' THEN t.verified_at_utc END) AS done_at,
             t.verified_at_utc, t.outlet_id, o.outlet_name,
             j.journey_reference, COALESCE(c.legal_name, c.display_name) AS customer_name
      FROM auditcore.p2_tasks t
      LEFT JOIN auditcore.journeys j ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
      LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
      LEFT JOIN auditcore.dealer_outlets o
        ON o.tenant_id = t.tenant_id AND o.outlet_id = COALESCE(t.outlet_id, j.outlet_id)
      WHERE t.tenant_id = :t AND t.created_at_utc < :e
        AND (t.task_status NOT IN ('CANCELLED', 'DEAD_LETTER') OR t.updated_at_utc >= :e)
        AND (t.scope_kind <> 'JOURNEY' OR upper(t.assigned_role_code) IN ('PC', 'TL', 'PM', 'EXECUTIVE', 'CRM'))
    )
    SELECT * FROM tasks
    WHERE created_at_utc >= :s
       OR done_at >= :s
       OR (done_at IS NULL AND task_status NOT IN ('VERIFIED_COMPLETE', 'CANCELLED', 'DEAD_LETTER'))
       OR (task_status = 'FAILED' AND due_at_utc >= :s AND due_at_utc < :e)
"""


def _empty_person() -> dict[str, Any]:
    return {"open": 0, "raisedOnDay": 0, "completedOnDay": 0, "completedLate": 0, "overdue": 0,
            "reviewOverdue": 0, "missed": 0, "incomplete": 0}


def build_task_status(connection: Connection, *, tenant_id: str, day: date,
                      now: datetime | None = None) -> dict[str, Any]:
    """The day's status of Journey Tasks and Task Manager tasks: per person
    (open, raised, completed, completed late, overdue, review overdue, missed,
    incomplete gate pass) and every flagged item. "At the end of the day" for a
    past day; now for today."""
    now = now or datetime.now(UTC)
    start, end = _bounds(day)
    cut = min(end, now)
    rows = connection.execute(text(_STATUS_SQL), {"t": tenant_id, "s": start, "e": end}).mappings().all()
    names = known_names(connection, tenant_id=tenant_id)
    sections: dict[str, dict[str, Any]] = {
        "JOURNEY": {"people": {}, "flags": [], "totals": _empty_person()},
        "TASK_MANAGER": {"people": {}, "flags": [], "totals": _empty_person()},
    }
    for row in rows:
        section = sections["JOURNEY" if row["scope_kind"] == "JOURNEY" else "TASK_MANAGER"]
        person = str(row["person"]) if row["person"] else f"role:{row['assigned_role']}"
        entry = section["people"].setdefault(person, {
            "actorId": row["person"], "name": names.get(str(row["person"] or "")),
            "role": normalise_role(row["assigned_role"]), **_empty_person(),
        })
        done_at, due = row["done_at"], row["due_at_utc"]
        done_by_cut = done_at is not None and done_at < cut
        flags: list[str] = []
        counts: list[str] = []
        if start <= row["created_at_utc"] < end:
            counts.append("raisedOnDay")
        if done_at is not None and start <= done_at < end:
            counts.append("completedOnDay")
            if due is not None and done_at > due:
                counts.append("completedLate")
                flags.append("CLOSED_LATE")
        open_at_cut = not done_by_cut and row["task_status"] not in ("CANCELLED", "DEAD_LETTER") and not (
            row["task_status"] == "FAILED" and row["completion_protocol"] != "SYSTEM_VERIFIED")
        incomplete = (row["task_status"] == "FAILED"
                      and (row["completion_result"] or {}).get("outcome") == "INCOMPLETE"
                      and due is not None and start <= due < end)
        if incomplete:
            counts.append("incomplete")
            flags.append("INCOMPLETE")
            open_at_cut = False
        if open_at_cut:
            counts.append("open")
            if due is not None and due < cut:
                counts.append("overdue")
                flags.append("OVERDUE")
            if row["missed_at_utc"] is not None and row["missed_at_utc"] < cut:
                counts.append("missed")
                flags.append("MISSED")
        review_due = row["review_due_at_utc"]
        if (row["completion_protocol"] == "REQUESTER_CONFIRMED" and done_by_cut and review_due is not None
                and review_due < cut and (row["verified_at_utc"] is None or row["verified_at_utc"] >= cut)
                and row["task_status"] not in ("CANCELLED", "RETURNED")):
            counts.append("reviewOverdue")
            flags.append("REVIEW_OVERDUE")
        for key in counts:
            entry[key] += 1
            section["totals"][key] += 1
        if flags and len(section["flags"]) < _FLAG_LIMIT:
            reviewer = row["reviewer_actor_id"]
            section["flags"].append({
                "taskId": str(row["task_id"]), "journeyId": str(row["journey_id"]) if row["journey_id"] else None,
                "journeyReference": row["journey_reference"], "customerName": row["customer_name"],
                "outletName": row["outlet_name"], "taskType": row["task_type"], "title": row["title"],
                "status": row["task_status"], "flags": flags, "assigneeActorId": row["person"],
                "assigneeName": names.get(str(row["person"] or "")),
                "assigneeRole": normalise_role(row["assigned_role"]),
                "reviewerRole": normalise_role(row["reviewer_role_code"]),
                "reviewerName": names.get(str(reviewer)) if reviewer else None,
                "dueAtUtc": _iso(due), "reviewDueAtUtc": _iso(review_due), "completedAtUtc": _iso(done_at),
                "recurring": row["schedule_id"] is not None,
            })
    out: dict[str, Any] = {"day": day.isoformat(), "asOfUtc": _iso(cut)}
    for key, label in (("JOURNEY", "journeyTasks"), ("TASK_MANAGER", "taskManager")):
        section = sections[key]
        people = sorted(section["people"].values(),
                        key=lambda p: (-(p["overdue"] + p["missed"] + p["incomplete"] + p["reviewOverdue"]),
                                       (p["name"] or p["actorId"] or "~").lower()))
        out[label] = {"totals": section["totals"], "people": people, "flags": section["flags"],
                      "flagsTruncated": len(section["flags"]) >= _FLAG_LIMIT}
    out["notes"] = [
        "Overdue: still open for the assignee after its expected time to close.",
        "Review overdue: completed but not accepted or sent back within 2 days (flagged against the reviewer).",
        "Missed: a recurring task still open when its next occurrence was raised.",
        "Incomplete: the daily gate pass was not finished by 23:59 the next day.",
        ("Journey tasks assigned to the PC role count against the PC who started the Journey; tasks for a whole "
         "role (Team Lead) show under that role."),
    ]
    return out


# --------------------------------------------------------------------------- time on Verigence


# Where the time was spent (2026-10-27): the browser on a computer, the browser
# on a phone or tablet, or the Android / iOS app.
CLIENT_KINDS = ("WEB", "MOBILE_WEB", "ANDROID", "IOS")


def record_activity(connection: Connection, *, tenant_id: str, caller: Caller, payload: dict[str, Any],
                    now: datetime | None = None) -> bool:
    """Raise the day's counters of the caller to what the browser reports.
    Only the caller's own row; only today or yesterday (IST); counters never
    go down, so a repeated or late send changes nothing."""
    now = now or datetime.now(UTC)
    try:
        day = date.fromisoformat(str(payload.get("date")))
    except ValueError as exc:
        raise TaskManagerError("Unknown date.") from exc
    today = now.astimezone(IST).date()
    if day not in (today, today - timedelta(days=1)):
        return False
    client = str(payload.get("clientKind") or "WEB").upper()
    if client not in CLIENT_KINDS:
        raise TaskManagerError("Unknown client.")

    def seconds(key: str) -> int:
        return max(0, min(int(payload.get(key) or 0), 86400))

    hourly = [max(0, min(int(v or 0), 60)) for v in list(payload.get("hourlyActiveMinutes") or [])[:24]]
    hourly += [0] * (24 - len(hourly))

    def stamp(key: str) -> datetime | None:
        value = payload.get(key)
        if not value:
            return None
        try:
            at = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if at.tzinfo is None:
            return None
        start, end = _bounds(day)
        return at if start <= at < end + timedelta(minutes=5) and at <= now + timedelta(minutes=5) else None

    sessions = clean_sessions(payload.get("sessions"), day=day, now=now)
    stored = _stored_day(connection, tenant_id=tenant_id, actor_id=caller.actor_id, day=day, client=client)
    minutes = merge_minute_states(stored["minute_states"], clean_minute_states(payload.get("minuteStates")))
    visits = merge_visits(stored["screen_visits"], clean_visits(payload.get("visits"), day=day, now=now))
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_user_activity_daily (
              tenant_id, actor_id, activity_date, client_kind, role_code, display_name,
              active_seconds, idle_seconds, background_seconds, hourly_active_minutes,
              first_active_at_utc, last_active_at_utc, sessions, minute_states, screen_visits
            ) VALUES (
              :t, :a, :d, :client, :role, :name, :active, :idle, :background, CAST(:hourly AS smallint[]),
              :first, :last, CAST(:sessions AS jsonb), :minutes, CAST(:visits AS jsonb)
            )
            ON CONFLICT (tenant_id, actor_id, activity_date, client_kind) DO UPDATE SET
              role_code = COALESCE(EXCLUDED.role_code, auditcore.p2_user_activity_daily.role_code),
              display_name = COALESCE(EXCLUDED.display_name, auditcore.p2_user_activity_daily.display_name),
              active_seconds = GREATEST(auditcore.p2_user_activity_daily.active_seconds, EXCLUDED.active_seconds),
              idle_seconds = GREATEST(auditcore.p2_user_activity_daily.idle_seconds, EXCLUDED.idle_seconds),
              background_seconds = GREATEST(auditcore.p2_user_activity_daily.background_seconds,
                                            EXCLUDED.background_seconds),
              hourly_active_minutes = ARRAY(
                SELECT GREATEST(o, n)::smallint
                FROM unnest(auditcore.p2_user_activity_daily.hourly_active_minutes,
                            EXCLUDED.hourly_active_minutes) AS x(o, n)),
              first_active_at_utc = LEAST(auditcore.p2_user_activity_daily.first_active_at_utc,
                                          EXCLUDED.first_active_at_utc),
              last_active_at_utc = GREATEST(auditcore.p2_user_activity_daily.last_active_at_utc,
                                            EXCLUDED.last_active_at_utc),
              sessions = CAST(:merged AS jsonb),
              minute_states = EXCLUDED.minute_states,
              screen_visits = EXCLUDED.screen_visits,
              updated_at_utc = now()
        """),
        {"t": tenant_id, "a": caller.actor_id, "d": day, "client": client, "role": caller.role,
         "name": (str(payload.get("displayName") or "").strip()[:200] or caller.display_name or None),
         "active": seconds("activeSeconds"), "idle": seconds("idleSeconds"),
         "background": seconds("backgroundSeconds"), "hourly": hourly,
         "first": stamp("firstActiveAt"), "last": stamp("lastActiveAt"),
         "sessions": json.dumps(sessions),
         "merged": json.dumps(merge_sessions(stored["sessions"], sessions)),
         "minutes": minutes, "visits": json.dumps(visits)},
    )
    return True


MAX_SESSIONS_PER_DAY = 100


def clean_sessions(raw: Any, *, day: date, now: datetime) -> list[dict[str, Any]]:
    """The browser's sessions of the day, kept only when they make sense:
    inside the day, start before end, not in the future."""
    start_of_day, end_of_day = _bounds(day)
    out: list[dict[str, Any]] = []
    for item in list(raw or [])[:MAX_SESSIONS_PER_DAY]:
        if not isinstance(item, dict):
            continue
        try:
            start = datetime.fromisoformat(str(item.get("start")))
            end = datetime.fromisoformat(str(item.get("end")))
        except ValueError:
            continue
        if start.tzinfo is None or end.tzinfo is None or end < start:
            continue
        if not (start_of_day - timedelta(minutes=5) <= start < end_of_day) or end > now + timedelta(minutes=5):
            continue
        active = max(0, min(int(item.get("activeSeconds") or 0), int((end - start).total_seconds()) + 60))
        out.append({"start": start.astimezone(UTC).isoformat(), "end": end.astimezone(UTC).isoformat(),
                    "activeSeconds": active})
    return out


def merge_sessions(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sessions are keyed by their start; a later send only ever lengthens one."""
    merged: dict[str, dict[str, Any]] = {str(s["start"]): dict(s) for s in old if s.get("start")}
    for item in new:
        current = merged.get(item["start"])
        if current is None:
            merged[item["start"]] = dict(item)
        else:
            current["end"] = max(str(current.get("end") or ""), item["end"])
            current["activeSeconds"] = max(int(current.get("activeSeconds") or 0), item["activeSeconds"])
    return sorted(merged.values(), key=lambda s: s["start"])[:MAX_SESSIONS_PER_DAY]


def _stored_day(connection: Connection, *, tenant_id: str, actor_id: str, day: date,
                client: str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT sessions, minute_states, screen_visits FROM auditcore.p2_user_activity_daily "
             "WHERE tenant_id = :t AND actor_id = :a AND activity_date = :d AND client_kind = :c FOR UPDATE"),
        {"t": tenant_id, "a": actor_id, "d": day, "c": client},
    ).mappings().one_or_none()
    return {"sessions": list((row or {}).get("sessions") or []),
            "minute_states": (row or {}).get("minute_states"),
            "screen_visits": list((row or {}).get("screen_visits") or [])}


# One character per minute of the IST day (2026-10-28): nothing, hidden, idle,
# active, and a click / tap / key in that very minute. Busiest wins on merge,
# so a repeated or late send never lowers a minute.
MINUTE_RANK = {".": 0, "B": 1, "I": 2, "A": 3, "K": 4}
MINUTES_PER_DAY = 1440


def clean_minute_states(raw: Any) -> str | None:
    value = str(raw or "")
    if len(value) != MINUTES_PER_DAY or any(c not in MINUTE_RANK for c in value):
        return None
    return value


def merge_minute_states(old: str | None, new: str | None) -> str | None:
    if not old:
        return new
    if not new:
        return old
    return "".join(a if MINUTE_RANK[a] >= MINUTE_RANK[b] else b for a, b in zip(old, new, strict=True))


MAX_VISITS_PER_DAY = 3000
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")


def clean_visits(raw: Any, *, day: date, now: datetime) -> list[dict[str, Any]]:
    """Screens opened in the browser: time, screen key, journey. Kept only
    inside the day and not in the future."""
    start_of_day, end_of_day = _bounds(day)
    out: list[dict[str, Any]] = []
    for item in list(raw or [])[:MAX_VISITS_PER_DAY]:
        if not isinstance(item, dict):
            continue
        try:
            at = datetime.fromisoformat(str(item.get("at")))
        except ValueError:
            continue
        if at.tzinfo is None or not (start_of_day <= at < end_of_day) or at > now + timedelta(minutes=5):
            continue
        screen = str(item.get("screen") or "").strip()[:160]
        if not screen:
            continue
        journey = str(item.get("journeyId") or "")
        out.append({"at": at.astimezone(UTC).isoformat(), "screen": screen,
                    "journeyId": journey if _UUID.match(journey) else None})
    return out


def merge_visits(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {(str(v.get("at")), str(v.get("screen"))): v for v in old if isinstance(v, dict)}
    for v in new:
        merged.setdefault((v["at"], v["screen"]), v)
    return sorted(merged.values(), key=lambda v: str(v.get("at")))[-MAX_VISITS_PER_DAY:]


# --------------------------------------------------------------------------- employee utilisation


def _empty_day() -> dict[str, Any]:
    return {
        "journeysStarted": 0, "filesUploaded": 0, "pagesUploaded": 0, "failedPages": 0,
        "tasksAssigned": 0, "tasksCompleted": 0, "tasksCompletedOnTime": 0, "sendBacksReceived": 0,
        "reviewsDone": 0, "journeyTasksWorked": 0,
        "activeMinutes": 0, "idleMinutes": 0, "backgroundMinutes": 0,
        "firstActiveAtUtc": None, "lastActiveAtUtc": None, "hourlyActiveMinutes": [0] * 24,
        "_completeMinutes": [],
    }


def build_utilisation(connection: Connection, *, tenant_id: str, start_day: date, end_day: date,
                      now: datetime | None = None) -> dict[str, Any]:
    """Per person of the Project and per IST day: output, task work, reviews
    and time on Verigence (product owner, 2026-10-26: Executive and Superadmin
    only -- the caller's right is checked by the route)."""
    now = now or datetime.now(UTC)
    if end_day < start_day:
        raise TaskManagerError("The end date is before the start date.")
    if (end_day - start_day).days + 1 > MAX_UTILISATION_DAYS:
        raise TaskManagerError(f"Choose at most {MAX_UTILISATION_DAYS} days.")
    start, _ = _bounds(start_day)
    _, end = _bounds(end_day)
    params = {"t": tenant_id, "s": start, "e": end}
    day_of = "((x.at AT TIME ZONE 'Asia/Kolkata')::date)"
    people = {p["actorId"]: p for p in project_people(connection, tenant_id=tenant_id)}
    names = known_names(connection, tenant_id=tenant_id)
    days: dict[str, dict[date, dict[str, Any]]] = defaultdict(dict)

    def cell(actor: Any, day: date) -> dict[str, Any]:
        return days[str(actor)].setdefault(day, _empty_day())

    def add(sql: str, key: str, *, value: str = "1") -> None:
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
        value="COALESCE(x.page_count, 0)")
    add("""SELECT b.uploaded_by_actor_id AS actor, b.created_at_utc AS at
           FROM auditcore.p2_document_queue q
           JOIN auditcore.p2_upload_batches b ON b.tenant_id = q.tenant_id AND b.batch_id = q.batch_id
           WHERE q.tenant_id = :t AND b.created_at_utc >= :s AND b.created_at_utc < :e
             AND q.queue_status IN ('FAILED', 'DEAD_LETTER')""", "failedPages")
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
               COUNT(DISTINCT te.task_id) AS n
        FROM auditcore.p2_task_events te
        WHERE te.tenant_id = :t AND te.journey_id IS NOT NULL AND te.created_at_utc >= :s AND te.created_at_utc < :e
          AND te.actor_id IS NOT NULL AND upper(te.actor_id) <> 'SYSTEM' AND te.event_type = ANY(:work)
        GROUP BY 1, 2
    """), {**params, "work": list(_WORK_EVENTS)}).mappings().all():
        cell(row["actor"], row["day"])["journeyTasksWorked"] += int(row["n"])
    for row in connection.execute(text("""
        SELECT completed_by_actor_id AS actor, ((action_completed_at_utc AT TIME ZONE 'Asia/Kolkata')::date) AS day,
               EXTRACT(EPOCH FROM (action_completed_at_utc - created_at_utc)) / 60 AS minutes
        FROM auditcore.p2_tasks
        WHERE tenant_id = :t AND scope_kind <> 'JOURNEY' AND completed_by_actor_id IS NOT NULL
          AND action_completed_at_utc >= :s AND action_completed_at_utc < :e
    """), params).mappings().all():
        cell(row["actor"], row["day"])["_completeMinutes"].append(float(row["minutes"] or 0))
    for row in connection.execute(text("""
        SELECT actor_id, activity_date, role_code, active_seconds, idle_seconds, background_seconds,
               hourly_active_minutes, first_active_at_utc, last_active_at_utc
        FROM auditcore.p2_user_activity_daily
        WHERE tenant_id = :t AND activity_date BETWEEN :sd AND :ed
    """), {"t": tenant_id, "sd": start_day, "ed": end_day}).mappings().all():
        c = cell(row["actor_id"], row["activity_date"])
        c["activeMinutes"] += round(int(row["active_seconds"]) / 60)
        c["idleMinutes"] += round(int(row["idle_seconds"]) / 60)
        c["backgroundMinutes"] += round(int(row["background_seconds"]) / 60)
        hourly = list(row["hourly_active_minutes"] or [])
        c["hourlyActiveMinutes"] = [min(60, a + int(b or 0)) for a, b in zip(c["hourlyActiveMinutes"], hourly + [0] * 24)]
        for key, pick in (("firstActiveAtUtc", min), ("lastActiveAtUtc", max)):
            value = row["first_active_at_utc" if key == "firstActiveAtUtc" else "last_active_at_utc"]
            if value is not None:
                c[key] = value if c[key] is None else pick(c[key], value)
    overdue_now = dict(connection.execute(text("""
        SELECT assigned_actor_id, COUNT(*) FROM auditcore.p2_tasks
        WHERE tenant_id = :t AND assigned_actor_id IS NOT NULL
          AND task_status IN ('READY', 'IN_PROGRESS', 'RETURNED') AND due_at_utc < :now
        GROUP BY 1
    """), {"t": tenant_id, "now": now}).all())
    review_overdue_now = dict(connection.execute(text("""
        SELECT reviewer_actor_id, COUNT(*) FROM auditcore.p2_tasks
        WHERE tenant_id = :t AND reviewer_actor_id IS NOT NULL AND task_status = 'AWAITING_REQUESTER_REVIEW'
          AND review_due_at_utc < :now
        GROUP BY 1
    """), {"t": tenant_id, "now": now}).all())

    span = [start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)]
    out_people = []
    for actor in sorted(set(people) | set(days)):
        if actor.upper() == "SYSTEM":
            continue
        per_day = days.get(actor, {})
        totals = _empty_day()
        series = []
        for d in span:
            c = per_day.get(d, _empty_day())
            for key, value in c.items():
                if key in ("firstActiveAtUtc", "lastActiveAtUtc"):
                    continue
                if key == "hourlyActiveMinutes":
                    totals[key] = [a + b for a, b in zip(totals[key], value)]
                elif key == "_completeMinutes":
                    totals[key] += value
                else:
                    totals[key] += value
            series.append({"date": d.isoformat(), **{k: (_iso(v) if k.endswith("Utc") else v)
                                                     for k, v in c.items() if not k.startswith("_")}})
        minutes = totals.pop("_completeMinutes")
        totals["avgMinutesToComplete"] = round(sum(minutes) / len(minutes)) if minutes else None
        totals.pop("firstActiveAtUtc", None)
        totals.pop("lastActiveAtUtc", None)
        totals["overdueNow"] = int(overdue_now.get(actor, 0))
        totals["reviewOverdueNow"] = int(review_overdue_now.get(actor, 0))
        totals["daysActive"] = sum(1 for s in series if s["activeMinutes"] > 0)
        person = people.get(actor)
        out_people.append({
            "actorId": actor, "name": (person or {}).get("displayName") or names.get(actor),
            "role": (person or {}).get("roleCode"), "activeInProject": person is not None,
            "totals": totals, "days": series,
        })
    role_order = {EXECUTIVE: 0, "PM": 1, "TL": 2, "CRM": 3, "REVIEWER": 4, "PC": 5}
    out_people.sort(key=lambda p: (role_order.get(p["role"] or "", 9), (p["name"] or p["actorId"]).lower()))
    return {
        "from": start_day.isoformat(), "to": end_day.isoformat(), "idleThresholdMinutes": IDLE_THRESHOLD_MINUTES,
        "people": out_people,
        "notes": [
            ("Time on Verigence is measured in the browser: active = a click, tap, key or scroll with the app "
             f"visible; idle = visible with no interaction for {IDLE_THRESHOLD_MINUTES} minutes or more; "
             "background = tab hidden or app minimised."),
            ("It measures interaction, not work: read it next to the output figures. Minutes since the last send "
             "are lost if the browser is closed abruptly (an undercount, never an overcount); someone using two "
             "browsers on the same day shows the larger of the two."),
            "Task figures cover Task Manager tasks; Journey tasks worked counts the Journey tasks a person acted on.",
            "Names show once the person has opened Verigence after this release, or from what they did.",
        ],
    }


def utilisation_allowed(caller: Caller) -> bool:
    return caller.is_super_admin or caller.role == EXECUTIVE

