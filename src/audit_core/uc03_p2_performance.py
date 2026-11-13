"""Performance: how the team and each person is doing, day by day (product owner, 13 Nov 2026).

"Some intuitive dashboard which helps to understand team efficiency, how the team is
performing, how each individual is performing ... for all PC/Reviewer/TL/PM/Exec, what
they do on daily basis and a relative comparison ... as compared to peers, as compared to
other days ... there should be some tasks which need to be done daily like gate register,
cash register, and some weekly; it should provision for all of those."

``GET /p2/v1/tenants/{t}/reports/performance?date=YYYY-MM-DD&days=14``

For the day asked and the ``days`` before it (7 to 31), per person and per day:

Output        filesUploaded, journeysUploaded (the PC who uploaded), tasksClosed (whoever
              closed them), reviews (Booking / Delivery documents-reviewed marks, Journey
              closures, and a TL's or PM's accept / reject of a closure)
Quality       pagesRejected: pages of that day's uploads rejected on image quality
Duties        every recurring task schedule of the Project (Daily Gate Pass today; a cash
              register or a weekly reconciliation appears on its own once its schedule is
              created in the Task Manager): due, done on time, done late, not done
Utilisation   activeMinutes: active time on Verigence (click, tap or key plus the reading
              window; the Employee engagement report's figure)

Each person is compared with their own usual (their average over the earlier working days
of the window, Sundays left out) and with their peers (the median, that day, of the people
of the same role who did anything that day). No combined score (product owner: a weighted
score later).

Who reads what (product owner): a Team Lead, PM, Executive or administrator reads the
people of their outlets by name; a PC or Reviewer reads their own row and the unnamed
peer median only.

Cost: six grouped reads over the window (each on an index by Project and time) and three
small look-ups, computed once per Project and window and kept for two minutes, whoever asks.
"""
from __future__ import annotations

import statistics
import threading
import time as _time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.task_manager import known_names, normalise_role
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_project_tracker import (
    _ADMIN_ROLES,
    _CLOSING_EVENTS,
    _FALLBACK_TIMEZONE,
    _MEMBER_ROLES,
    _REPORT_ROLES,
    _holds,
    _scope,
    _zone,
    period_bounds,
)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-performance"])

_READ_PERMISSION = "audit.journey.read"
MIN_DAYS, MAX_DAYS, DEFAULT_DAYS = 7, 31, 14
_CACHE_SECONDS = 120.0

# The measures, in the order shown. ``roles``: whose work it is (the screen shows it for them).
MEASURES: tuple[dict[str, Any], ...] = (
    {"key": "filesUploaded", "label": "Files uploaded", "group": "OUTPUT", "roles": ["PC"]},
    {"key": "journeysUploaded", "label": "Journeys uploaded", "group": "OUTPUT", "roles": ["PC"]},
    {"key": "reviews", "label": "Reviews", "group": "OUTPUT", "roles": ["REVIEWER", "TL", "PM", "EXECUTIVE"]},
    {"key": "tasksClosed", "label": "Tasks closed", "group": "OUTPUT",
     "roles": ["PC", "REVIEWER", "TL", "PM", "EXECUTIVE"]},
    {"key": "pagesRejected", "label": "Pages rejected", "group": "QUALITY", "roles": ["PC"], "lowerIsBetter": True},
    {"key": "activeMinutes", "label": "Active time", "group": "UTILISATION", "unit": "MINUTES",
     "roles": ["PC", "REVIEWER", "TL", "PM", "EXECUTIVE"]},
)
_KEYS = tuple(m["key"] for m in MEASURES)
_LOWER = frozenset(m["key"] for m in MEASURES if m.get("lowerIsBetter"))

# Duties the product owner has named whose details are still to come: shown as "not set up
# yet" until their schedule exists in the Task Manager (13 Nov 2026: "Cash register details I
# share later, let's keep placeholder for it").
PLANNED_DUTIES: tuple[dict[str, str], ...] = (
    {"title": "Cash register", "frequency": "DAILY"},
)

# Within a tenth of the peers' median counts as "with peers".
_BAND = 0.10

_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _aggregate(connection: Connection, *, tenant_id: str, first: date, last: date, tz: str,
               now: datetime) -> dict[str, Any]:
    """Everything of the Project in the window, per person and day. Six grouped reads."""
    start, end = period_bounds(first, last, tz)
    params = {"t": tenant_id, "s": start, "e": end, "tz": tz}
    values: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))

    def put(rows: Any, key: str, column: str = "n") -> None:
        for r in rows:
            if r["actor"]:
                cell = values[str(r["actor"])][key]
                day = r["day"].isoformat()
                cell[day] = cell.get(day, 0) + float(r[column] or 0)

    uploads = connection.execute(text("""
        SELECT uploaded_by_actor_id AS actor, (created_at_utc AT TIME ZONE :tz)::date AS day,
               count(*) AS files, count(DISTINCT journey_id) AS journeys
        FROM auditcore.p2_upload_batches
        WHERE tenant_id = :t AND created_at_utc >= :s AND created_at_utc < :e
          AND batch_status <> 'AWAITING_UPLOAD'
          AND NOT (batch_status = 'CANCELLED' AND COALESCE(page_count, 0) = 0)
        GROUP BY 1, 2
    """), params).mappings().all()
    put(uploads, "filesUploaded", "files")
    put(uploads, "journeysUploaded", "journeys")
    put(connection.execute(text("""
        SELECT ub.uploaded_by_actor_id AS actor, (ub.created_at_utc AT TIME ZONE :tz)::date AS day,
               sum(COALESCE(cardinality(q.page_numbers), 1)) AS n
        FROM auditcore.p2_upload_batches ub
        JOIN auditcore.p2_document_queue q ON q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
        WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
          AND q.last_error LIKE 'DI\\_QUALITY\\_%' AND q.queue_status IN ('FAILED', 'DEAD_LETTER', 'CANCELLED')
        GROUP BY 1, 2
    """), params).mappings().all(), "pagesRejected")
    put(connection.execute(text("""
        SELECT actor_id AS actor, (created_at_utc AT TIME ZONE :tz)::date AS day, count(DISTINCT task_id) AS n
        FROM auditcore.p2_task_events
        WHERE tenant_id = :t AND created_at_utc >= :s AND created_at_utc < :e
          AND actor_id IS NOT NULL AND upper(actor_id) <> 'SYSTEM' AND event_type = ANY(:closing)
        GROUP BY 1, 2
    """), {**params, "closing": sorted(_CLOSING_EVENTS)}).mappings().all(), "tasksClosed")
    put(connection.execute(text("""
        SELECT actor, day, count(*) AS n FROM (
          SELECT reviewed_by_actor_id AS actor, (reviewed_at_utc AT TIME ZONE :tz)::date AS day
          FROM auditcore.p2_stage_document_reviews
          WHERE tenant_id = :t AND reviewed_at_utc >= :s AND reviewed_at_utc < :e
          UNION ALL
          SELECT closed_by_actor_id, (closed_at_utc AT TIME ZONE :tz)::date
          FROM auditcore.p2_journey_closures
          WHERE tenant_id = :t AND closed_at_utc >= :s AND closed_at_utc < :e
          UNION ALL
          SELECT decided_by_actor_id, (decided_at_utc AT TIME ZONE :tz)::date
          FROM auditcore.p2_journey_closures
          WHERE tenant_id = :t AND decided_at_utc >= :s AND decided_at_utc < :e
            AND acceptance_status IN ('ACCEPTED', 'REJECTED')
        ) x GROUP BY 1, 2
    """), params).mappings().all(), "reviews")
    put(connection.execute(text("""
        SELECT actor_id AS actor, activity_date AS day, round(sum(active_seconds) / 60.0) AS n
        FROM auditcore.p2_user_activity_daily
        WHERE tenant_id = :t AND activity_date BETWEEN :first AND :last
        GROUP BY 1, 2
    """), {"t": tenant_id, "first": first, "last": last}).mappings().all(), "activeMinutes")

    # Duties: every occurrence of every recurring schedule in the window, by state.
    duties: dict[str, dict[str, dict[str, dict[str, int]]]] = defaultdict(lambda: defaultdict(dict))
    for r in connection.execute(text("""
        SELECT COALESCE(t.assigned_actor_id, t.completed_by_actor_id) AS actor, t.schedule_id, t.occurrence_date AS day,
               CASE
                 WHEN d.done_at IS NOT NULL AND (t.completion_result->>'outcome' = 'DONE_LATE'
                      OR (t.due_at_utc IS NOT NULL AND d.done_at > t.due_at_utc)) THEN 'late'
                 WHEN d.done_at IS NOT NULL THEN 'onTime'
                 WHEN t.task_status = 'FAILED' OR (t.due_at_utc IS NOT NULL AND t.due_at_utc < :now) THEN 'notDone'
                 ELSE 'pending'
               END AS state, count(*) AS n
        FROM auditcore.p2_tasks t
        CROSS JOIN LATERAL (SELECT COALESCE(t.action_completed_at_utc,
            CASE WHEN t.task_status = 'VERIFIED_COMPLETE' THEN COALESCE(t.verified_at_utc, t.updated_at_utc) END)
            AS done_at) d
        WHERE t.tenant_id = :t AND t.schedule_id IS NOT NULL AND t.occurrence_date BETWEEN :first AND :last
          AND t.task_status NOT IN ('CANCELLED', 'DEAD_LETTER')
        GROUP BY 1, 2, 3, 4
    """), {"t": tenant_id, "first": first, "last": last, "now": now}).mappings():
        if r["actor"]:
            cell = duties[str(r["actor"])][str(r["schedule_id"])].setdefault(
                r["day"].isoformat(), {"due": 0, "onTime": 0, "late": 0, "notDone": 0, "pending": 0})
            cell["due"] += int(r["n"])
            cell[r["state"]] += int(r["n"])

    schedules = [
        {"scheduleId": str(r["schedule_id"]), "title": r["title"], "frequency": r["frequency"],
         "status": r["schedule_status"], "assignedRole": normalise_role(r["assigned_role_code"])}
        for r in connection.execute(text("""
            SELECT schedule_id, title, frequency, schedule_status, assigned_role_code
            FROM auditcore.p2_task_schedules
            WHERE tenant_id = :t AND schedule_status <> 'ARCHIVED'
            ORDER BY (frequency = 'DAILY') DESC, lower(title)
        """), {"t": tenant_id}).mappings()
    ]
    # Each person's operating role and where they are mapped (a mapping with no dealer: the Project).
    people: dict[str, dict[str, Any]] = {}
    for r in connection.execute(text("""
        SELECT ba.security_actor_id AS actor, upper(ba.business_role_code) AS role, ba.dealer_id, ba.outlet_id
        FROM auditcore.business_assignments ba
        WHERE ba.tenant_id = :t AND ba.assignment_status = 'ACTIVE'
          AND ba.effective_from <= now() AND (ba.effective_to IS NULL OR ba.effective_to >= now())
    """), {"t": tenant_id}).mappings():
        role = normalise_role(r["role"]) or ""
        person = people.setdefault(str(r["actor"]), {"role": role, "wide": False, "dealers": set(), "outlets": set()})
        if role == "PC":
            person["role"] = role  # a PC mapping names the row, as in the Project Tracker
        if r["dealer_id"] is None:
            person["wide"] = True
        elif r["outlet_id"] is None:
            person["dealers"].add(str(r["dealer_id"]))
        else:
            person["outlets"].add(str(r["outlet_id"]))
    return {"values": values, "duties": duties, "schedules": schedules, "people": people,
            "names": known_names(connection, tenant_id=tenant_id)}


def _cached(connection: Connection, *, tenant_id: str, first: date, last: date, tz: str, now: datetime,
            use_cache: bool) -> dict[str, Any]:
    key = (tenant_id, first.isoformat(), last.isoformat())
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and _time.monotonic() - hit[0] < _CACHE_SECONDS:
                return hit[1]
    data = _aggregate(connection, tenant_id=tenant_id, first=first, last=last, tz=tz, now=now)
    if use_cache:
        with _cache_lock:
            if len(_cache) > 64:
                _cache.clear()
            _cache[key] = (_time.monotonic(), data)
    return data


def standing(value: float, median: float | None, *, lower_is_better: bool = False) -> str | None:
    """above / with / below the peers' median (within a tenth is "with"); None with no peers to
    compare with. For a measure where lower is better, fewer is "above"."""
    if median is None:
        return None
    if abs(value - median) <= max(_BAND * median, 0.5):
        return "with"
    better = value < median if lower_is_better else value > median
    return "above" if better else "below"


def build_performance(
    connection: Connection, *, tenant_id: str, actor_id: str, day: date, days: int = DEFAULT_DAYS,
    admin: bool = False, own: bool = False, now: datetime | None = None, use_cache: bool = True,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    tz = connection.execute(
        text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant_id},
    ).scalar_one_or_none() or _FALLBACK_TIMEZONE
    today = now.astimezone(_zone(tz)).date()
    if day > today:
        raise HTTPException(status_code=422, detail="That day has not come yet.")
    days = max(MIN_DAYS, min(MAX_DAYS, days))
    first = day - timedelta(days=days - 1)
    window = _days(first, day)
    iso = [d.isoformat() for d in window]
    target = day.isoformat()
    # The person's usual: the earlier working days of the window (Sunday is the off day).
    usual_days = [d.isoformat() for d in window[:-1] if d.weekday() != 6]
    data = _cached(connection, tenant_id=tenant_id, first=first, last=day, tz=tz, now=now, use_cache=use_cache)
    values, duties, people = data["values"], data["duties"], data["people"]

    def series(actor: str, key: str) -> list[float]:
        cell = values.get(actor, {}).get(key, {})
        return [cell.get(d, 0) for d in iso]

    def usual(actor: str, key: str) -> float | None:
        cell = values.get(actor, {}).get(key, {})
        return round(sum(cell.get(d, 0) for d in usual_days) / len(usual_days), 1) if usual_days else None

    def active(actor: str, d: str) -> bool:
        return any(values.get(actor, {}).get(k, {}).get(d, 0) > 0 for k in _KEYS)

    # Peers: the median, that day, of the people of the same role who did anything that day;
    # and of their usual, among those who did anything in the window.
    by_role: dict[str, list[str]] = defaultdict(list)
    for actor, person in people.items():
        if person["role"]:
            by_role[person["role"]].append(actor)
    peers: dict[str, dict[str, Any]] = {}
    for role, actors in by_role.items():
        present = [a for a in actors if active(a, target)]
        seen = [a for a in actors if any(active(a, d) for d in iso)]
        peers[role] = {
            "people": len(actors), "activeToday": len(present),
            "median": {k: (statistics.median(values.get(a, {}).get(k, {}).get(target, 0) for a in present)
                           if present else None) for k in _KEYS},
            "usualMedian": {k: (round(statistics.median(usual(a, k) or 0 for a in seen), 1)
                                if seen and usual_days else None) for k in _KEYS},
        }

    # Whom the caller reads: their own row; or the people of their outlets.
    if own:
        visible = [actor_id] if actor_id in people else []
    else:
        wide, outlets = _scope(connection, tenant_id=tenant_id, actor_id=actor_id, admin=admin)
        outlet_ids = {str(o["outlet_id"]) for o in outlets}
        dealer_ids = {str(o["dealer_id"]) for o in outlets}
        visible = [a for a, p in people.items()
                   if wide or p["outlets"] & outlet_ids or p["dealers"] & dealer_ids]

    def duty_totals(actor: str, only: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for schedule_id, by_day in duties.get(actor, {}).items():
            total = {"due": 0, "onTime": 0, "late": 0, "notDone": 0, "pending": 0}
            for d, cell in by_day.items():
                if only is None or d == only:
                    for k in total:
                        total[k] += cell[k]
            if total["due"]:
                out[schedule_id] = total
        return out

    rows = []
    for actor in visible:
        person = people[actor]
        role = person["role"]
        peer = peers.get(role, {})
        today_values = {k: values.get(actor, {}).get(k, {}).get(target, 0) for k in _KEYS}
        rows.append({
            "actorId": actor, "name": data["names"].get(actor), "role": role,
            "today": today_values,
            "usual": {k: usual(actor, k) for k in _KEYS},
            "series": {k: series(actor, k) for k in _KEYS},
            "standing": {k: standing(today_values[k], (peer.get("median") or {}).get(k), lower_is_better=k in _LOWER)
                         if active(actor, target) else None for k in _KEYS},
            "activeToday": active(actor, target),
            "dutiesToday": duty_totals(actor, target),
            "dutiesWindow": duty_totals(actor, None),
        })
    rows.sort(key=lambda r: (r["role"] or "~", (r["name"] or r["actorId"]).lower()))
    return {
        "date": target, "first": first.isoformat(), "days": iso, "timezone": tz, "current": day == today,
        "asOfUtc": now.isoformat(), "own": own,
        "measures": [dict(m) for m in MEASURES],
        "schedules": data["schedules"],
        "plannedDuties": [d for d in PLANNED_DUTIES
                          if not any(s["title"].strip().lower() == d["title"].lower() for s in data["schedules"])],
        "peers": peers,
        "people": rows,
        "notes": [
            ("Today's figure against the person's usual (their average over the earlier working days shown, "
            "Sundays left out) and against peers (the median, that day, of the people of the same role who "
            "did anything that day). Within a tenth of the median counts as 'with peers'."),
            ("Files and journeys uploaded count under the PC who uploaded them; tasks closed under whoever "
            "closed them; reviews are Booking / Delivery documents-reviewed marks, Journey closures and a "
            "TL's or PM's accept or reject of a closure. Pages rejected are pages of that day's uploads "
            "rejected on image quality (fewer is better)."),
            ("Duties are the recurring tasks of the Task Manager (daily, weekly or monthly): done on time, "
            "done late or not done. A new duty appears here once its schedule is created."),
            ("Active time is the time on Verigence with a click, tap or key and the reading window after it."),
        ],
    }


@router.get("/reports/performance")
def get_performance(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    day: Annotated[date | None, Query(alias="date")] = None,
    days: Annotated[int, Query(ge=MIN_DAYS, le=MAX_DAYS)] = DEFAULT_DAYS,
) -> dict[str, Any]:
    access = authorize_p2(
        connection, tenant_id=tenant_id, journey_id=None, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_READ_PERMISSION,
    )
    admin = (access.functional_role or "").upper() in _ADMIN_ROLES
    team = admin or _holds(connection, tenant_id, human_principal.subject, _REPORT_ROLES)
    if not team and not _holds(connection, tenant_id, human_principal.subject, _MEMBER_ROLES):
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
    if day is None:
        tz = connection.execute(
            text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant_id},
        ).scalar_one_or_none()
        day = datetime.now(UTC).astimezone(_zone(tz)).date()
    return build_performance(connection, tenant_id=tenant_id, actor_id=human_principal.subject, day=day,
                             days=days, admin=admin, own=not team)
