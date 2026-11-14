"""Performance: how the team and each person is doing, day by day (product owner, 13 Nov 2026).

"Some intuitive dashboard which helps to understand team efficiency, how the team is
performing, how each individual is performing ... for all PC/Reviewer/TL/PM/Exec, what
they do on daily basis and a relative comparison ... as compared to peers, as compared to
other days ... there should be some tasks which need to be done daily like gate register,
cash register, and some weekly; it should provision for all of those."

``GET /p2/v1/tenants/{t}/reports/performance?date=YYYY-MM-DD&days=14``

For the day asked and the ``days`` before it (7 to 31), per person and per day:

Output        filesUploaded, journeysUploaded (the PC who uploaded), journeysClosed (a PC's
              delivery files closed; the Journeys a Reviewer, TL or PM closed), tasksClosed
              (whoever closed them), reviews (Booking / Delivery documents-reviewed marks and
              a TL's or PM's accept / reject of a closure)
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
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_platform_super_admin_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_human_principal,
    require_super_admin_request,
)
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.task_manager import known_names, normalise_role
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_daily_report import delivery_files_closed
from audit_core.uc03_p2_dashboard import WIDGETS as DASHBOARD_WIDGETS
from audit_core.uc03_p2_dashboard import build as build_dashboard
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
MIN_DAYS, MAX_DAYS, DEFAULT_DAYS = 1, 92, 14  # a date range of a day up to about three months
_CACHE_SECONDS = 120.0

# The measures, in the order shown. ``roles``: whose work it is (the screen shows it for them).
MEASURES: tuple[dict[str, Any], ...] = (
    {"key": "filesUploaded", "label": "Files uploaded", "group": "OUTPUT", "roles": ["PC"]},
    {"key": "journeysUploaded", "label": "Journeys uploaded", "group": "OUTPUT", "roles": ["PC"]},
    # Product owner 13 Nov 2026: "it should also have journeys closed". A PC's: their delivery
    # files closed (all mandatory documents in, every document task closed); a Reviewer's, TL's or
    # PM's: the Journeys they closed.
    {"key": "journeysClosed", "label": "Journeys closed", "group": "OUTPUT",
     "roles": ["PC", "REVIEWER", "TL", "PM", "EXECUTIVE"]},
    {"key": "reviews", "label": "Reviews", "group": "OUTPUT", "roles": ["REVIEWER", "TL", "PM", "EXECUTIVE"]},
    {"key": "tasksClosed", "label": "Tasks closed", "group": "OUTPUT",
     "roles": ["PC", "REVIEWER", "TL", "PM", "EXECUTIVE"]},
    {"key": "pagesRejected", "label": "Pages rejected", "group": "QUALITY", "roles": ["PC"], "lowerIsBetter": True},
    {"key": "activeMinutes", "label": "Active time", "group": "UTILISATION", "unit": "MINUTES",
     "roles": ["PC", "REVIEWER", "TL", "PM", "EXECUTIVE"]},
)
# The measures of an outlet or a dealership: the same facts by the outlet they belong to. Active
# time is a person's, not an outlet's; a PC's delivery files closed and a Reviewer's closures are
# two different things at an outlet, so they are shown apart.
UNIT_MEASURES: tuple[dict[str, Any], ...] = (
    {"key": "filesUploaded", "label": "Files uploaded", "group": "OUTPUT"},
    {"key": "journeysUploaded", "label": "Journeys uploaded", "group": "OUTPUT"},
    {"key": "deliveryFilesClosed", "label": "Delivery files closed", "group": "OUTPUT", "setting": "journeysClosed"},
    {"key": "journeysClosed", "label": "Journeys closed by Reviewer", "group": "OUTPUT"},
    {"key": "reviews", "label": "Reviews", "group": "OUTPUT"},
    {"key": "tasksClosed", "label": "Tasks closed", "group": "OUTPUT"},
    {"key": "pagesRejected", "label": "Pages rejected", "group": "QUALITY", "lowerIsBetter": True},
)
# Whose work a lead reads (product owner 13 Nov 2026: "for TL and up, they also want to see how
# Reviewers are working and same for PM how TL are working"): the roles below their own.
_ROLE_RANK = {"PC": 0, "REVIEWER": 1, "CRM": 1, "TL": 2, "PM": 3, "EXECUTIVE": 4}
_KEYS = tuple(m["key"] for m in MEASURES)
_LOWER = frozenset(m["key"] for m in MEASURES if m.get("lowerIsBetter"))

# Duties the product owner has named whose details are still to come: shown as "not set up
# yet" until their schedule exists in the Task Manager (13 Nov 2026: "Cash register details I
# share later, let's keep placeholder for it").
PLANNED_DUTIES: tuple[dict[str, str], ...] = (
    {"title": "Cash register", "frequency": "DAILY"},
)

# What a SuperAdmin can switch off in the view (product owner 13 Nov 2026: "make the parameters
# to be displayed configurable under Superadmin control, like utilization and all"): each
# measure, the duties, and the comparison with peers. Anything not switched is shown.
DUTIES_ITEM, PEERS_ITEM = "duties", "peerComparison"
# The sections of the screen (product owner 13 Nov 2026: pending work, PC efficiency, day to day
# and week to week, delinquencies, bookings and deliveries).
SECTIONS: tuple[dict[str, str], ...] = (
    {"key": "pending", "label": "Backlog"},
    {"key": "flow", "label": "Bookings and deliveries"},
    {"key": "uploads", "label": "Uploads (direct, bulk, Drive)"},
    {"key": "quality", "label": "Duplicates and quality issues"},
    {"key": "tasks", "label": "Tasks"},
    {"key": "trend", "label": "Day to day, week to week"},
    {"key": "efficiency", "label": "PC efficiency"},
    {"key": DUTIES_ITEM, "label": "Delinquencies (daily and weekly duties)"},
)
SETTING_ITEMS: tuple[dict[str, str], ...] = (
    *({"key": x["key"], "label": x["label"], "group": "SECTION"} for x in SECTIONS),
    *({"key": m["key"], "label": m["label"], "group": m["group"]} for m in MEASURES),
    {"key": PEERS_ITEM, "label": "Comparison with peers", "group": "COMPARISON"},
    *({"key": w["key"], "label": w["label"], "group": "DASHBOARD"} for w in DASHBOARD_WIDGETS),
)
# Each item is switched per role (product owner 13 Nov 2026: "I want superadmin to be able to
# control what TL can see and what PM can see"). Stored as "<ROLE>:<item>"; an item with no role
# (the first version's rows, none were ever set) is ignored.
SETTING_ROLES = ("PC", "REVIEWER", "TL", "PM", "EXECUTIVE")


def hidden_items(connection: Connection, role: str | None = None) -> set[str]:
    """The items a SuperAdmin has switched off for this role (one read of a table of a few rows).
    ``role`` None: an administrator, who sees everything."""
    if role is None:
        return set()
    out: set[str] = set()
    for (key,) in connection.execute(text("SELECT item_key FROM platform_ops.performance_settings WHERE NOT shown")):
        owner, _, item = str(key).rpartition(":")
        if owner == role:
            out.add(item)
    return out


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
    # The same facts by the outlet they belong to (the Journey's or the task's outlet), whoever did
    # them: the outlet and dealership views (product owner 13 Nov 2026: "I need to see dealership,
    # PC, outlet views").
    by_outlet: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))

    def put(rows: Any, key: str, column: str = "n") -> None:
        for r in rows:
            day = r["day"].isoformat()
            amount = float(r[column] or 0)
            if r["actor"]:
                cell = values[str(r["actor"])][key]
                cell[day] = cell.get(day, 0) + amount
            if r.get("outlet"):
                cell = by_outlet[str(r["outlet"])][key]
                cell[day] = cell.get(day, 0) + amount

    uploads = connection.execute(text("""
        SELECT ub.uploaded_by_actor_id AS actor, (ub.created_at_utc AT TIME ZONE :tz)::date AS day, j.outlet_id AS outlet,
               count(*) AS files, count(DISTINCT ub.journey_id) AS journeys, sum(COALESCE(ub.page_count, 0)) AS pages
        FROM auditcore.p2_upload_batches ub
        JOIN auditcore.journeys j ON j.tenant_id = ub.tenant_id AND j.journey_id = ub.journey_id
        WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
          AND ub.batch_status <> 'AWAITING_UPLOAD'
          AND NOT (ub.batch_status = 'CANCELLED' AND COALESCE(ub.page_count, 0) = 0)
        GROUP BY 1, 2, 3
    """), params).mappings().all()
    put(uploads, "filesUploaded", "files")
    put(uploads, "journeysUploaded", "journeys")
    put(uploads, "pagesUploaded", "pages")
    put(connection.execute(text("""
        SELECT ub.uploaded_by_actor_id AS actor, (ub.created_at_utc AT TIME ZONE :tz)::date AS day, j.outlet_id AS outlet,
               sum(COALESCE(cardinality(q.page_numbers), 1)) AS n
        FROM auditcore.p2_upload_batches ub
        JOIN auditcore.journeys j ON j.tenant_id = ub.tenant_id AND j.journey_id = ub.journey_id
        JOIN auditcore.p2_document_queue q ON q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
        WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
          AND q.last_error LIKE 'DI\\_QUALITY\\_%' AND q.queue_status IN ('FAILED', 'DEAD_LETTER', 'CANCELLED')
        GROUP BY 1, 2, 3
    """), params).mappings().all(), "pagesRejected")
    put(connection.execute(text("""
        SELECT te.actor_id AS actor, (te.created_at_utc AT TIME ZONE :tz)::date AS day,
               COALESCE(tk.outlet_id, j.outlet_id) AS outlet, count(DISTINCT te.task_id) AS n
        FROM auditcore.p2_task_events te
        JOIN auditcore.p2_tasks tk ON tk.tenant_id = te.tenant_id AND tk.task_id = te.task_id
        LEFT JOIN auditcore.journeys j ON j.tenant_id = tk.tenant_id AND j.journey_id = tk.journey_id
        WHERE te.tenant_id = :t AND te.created_at_utc >= :s AND te.created_at_utc < :e
          AND te.actor_id IS NOT NULL AND upper(te.actor_id) <> 'SYSTEM' AND te.event_type = ANY(:closing)
        GROUP BY 1, 2, 3
    """), {**params, "closing": sorted(_CLOSING_EVENTS)}).mappings().all(), "tasksClosed")
    put(connection.execute(text("""
        SELECT actor, day, outlet, count(*) AS n FROM (
          SELECT reviewed_by_actor_id AS actor, (reviewed_at_utc AT TIME ZONE :tz)::date AS day, outlet_id AS outlet
          FROM auditcore.p2_stage_document_reviews
          WHERE tenant_id = :t AND reviewed_at_utc >= :s AND reviewed_at_utc < :e
          UNION ALL
          SELECT c.decided_by_actor_id, (c.decided_at_utc AT TIME ZONE :tz)::date, j.outlet_id
          FROM auditcore.p2_journey_closures c
          JOIN auditcore.journeys j ON j.tenant_id = c.tenant_id AND j.journey_id = c.journey_id
          WHERE c.tenant_id = :t AND c.decided_at_utc >= :s AND c.decided_at_utc < :e
            AND c.acceptance_status IN ('ACCEPTED', 'REJECTED')
        ) x GROUP BY 1, 2, 3
    """), params).mappings().all(), "reviews")
    put(connection.execute(text("""
        SELECT c.closed_by_actor_id AS actor, (c.closed_at_utc AT TIME ZONE :tz)::date AS day, j.outlet_id AS outlet,
               count(*) AS n
        FROM auditcore.p2_journey_closures c
        JOIN auditcore.journeys j ON j.tenant_id = c.tenant_id AND j.journey_id = c.journey_id
        WHERE c.tenant_id = :t AND c.closed_at_utc >= :s AND c.closed_at_utc < :e
        GROUP BY 1, 2, 3
    """), params).mappings().all(), "journeysClosed")
    # A PC's delivery files closed, by the Daily processing report's rule (one statement for the Project).
    zone = _zone(tz)
    for f in delivery_files_closed(connection, tenant_id=tenant_id, actor_id="", start=start, end=end, names={},
                                   whole_project=True)["files"]:
        if f["pcActorId"] and f["closedAtUtc"]:
            day = datetime.fromisoformat(f["closedAtUtc"]).astimezone(zone).date().isoformat()
            cell = values[str(f["pcActorId"])]["journeysClosed"]
            cell[day] = cell.get(day, 0) + 1
            if f.get("outletId"):
                cell = by_outlet[str(f["outletId"])]["deliveryFilesClosed"]
                cell[day] = cell.get(day, 0) + 1
    put(connection.execute(text("""
        SELECT actor_id AS actor, activity_date AS day, round(sum(active_seconds) / 60.0) AS n
        FROM auditcore.p2_user_activity_daily
        WHERE tenant_id = :t AND activity_date BETWEEN :first AND :last
        GROUP BY 1, 2
    """), {"t": tenant_id, "first": first, "last": last}).mappings().all(), "activeMinutes")

    # Duties: every occurrence of every recurring schedule in the window, by state.
    duties: dict[str, dict[str, dict[str, dict[str, int]]]] = defaultdict(lambda: defaultdict(dict))
    outlet_duties: dict[str, dict[str, dict[str, dict[str, int]]]] = defaultdict(lambda: defaultdict(dict))
    for r in connection.execute(text("""
        SELECT COALESCE(t.assigned_actor_id, t.completed_by_actor_id) AS actor, t.schedule_id, t.occurrence_date AS day,
               t.outlet_id AS outlet,
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
        GROUP BY 1, 2, 3, 4, 5
    """), {"t": tenant_id, "first": first, "last": last, "now": now}).mappings():
        for owner, key in ((duties, r["actor"]), (outlet_duties, r["outlet"])):
            if key:
                cell = owner[str(key)][str(r["schedule_id"])].setdefault(
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
    return {"values": values, "duties": duties, "by_outlet": by_outlet, "outlet_duties": outlet_duties,
            "schedules": schedules, "people": people,
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


def _sections(part: Any, data: dict[str, Any], *, rows: list[dict[str, Any]], outlets: list[dict[str, Any]],
              own: bool, hidden: set[str], iso: list[str], people: dict[str, Any], only: str | None) -> dict[str, Any]:
    """What a lead manages by, for the people and outlets the caller reads, leaving out what a
    SuperAdmin has switched off for their role. ``only``: one section (the tab that is open), so
    nothing else is read or sent."""
    from audit_core.uc03_p2_team_insights import BUCKETS, PENDING_KINDS

    values, duties = data["values"], data["duties"]
    visible = [r["actorId"] for r in rows]
    names = {r["actorId"]: r["name"] for r in rows}
    out: dict[str, Any] = {"sections": [x for x in SECTIONS if x["key"] not in hidden]}

    def wanted(key: str) -> bool:
        return key not in hidden and (only is None or only == key)

    def place(o: dict[str, Any]) -> dict[str, Any]:
        return {"outletId": str(o["outlet_id"]), "outletName": o["outlet_name"], "dealerId": str(o["dealer_id"]),
                "dealerName": o["dealer_name"]}

    if wanted("pending"):
        backlog = part("backlog")
        # A person's backlog is their own; an outlet's is everything at the outlet.
        kinds = [k for k in PENDING_KINDS if not own or k["key"] not in ("awaitingReview",)]
        out["pending"] = {
            "kinds": kinds, "buckets": list(BUCKETS),
            "outlets": [] if own else [{**place(o), "counts": backlog["byOutlet"].get(str(o["outlet_id"]), {})}
                                       for o in outlets],
            "people": [{"actorId": a, "name": names[a], "role": people[a]["role"],
                        "counts": backlog["byPerson"].get(a, {})} for a in visible],
        }

    if wanted("flow") and not own:
        flow = part("flow")["byOutlet"]
        out["flow"] = {"outlets": [
            {**place(o), "days": {d: flow.get(str(o["outlet_id"]), {}).get(d) for d in iso
                                  if flow.get(str(o["outlet_id"]), {}).get(d)}}
            for o in outlets]}

    if wanted("uploads"):
        ch = part("channels")
        # Duplicates and files with errors per channel (product owner 2026-11-14), totals of the period.
        issues = part("channelIssues")
        no_issues = {"duplicates": dict.fromkeys(("direct", "bulk", "drive"), 0),
                     "errors": dict.fromkeys(("direct", "bulk", "drive"), 0)}

        def by_channel(source: dict[str, Any], problems: dict[str, Any]) -> dict[str, Any]:
            return {**{c: [source.get(c, {}).get(d, 0) for d in iso] for c in ("direct", "bulk", "drive")},
                    "duplicates": dict(problems["duplicates"]), "errors": dict(problems["errors"])}

        out["uploads"] = {
            "outlets": [] if own else [
                {**place(o), **by_channel(ch["byOutlet"].get(str(o["outlet_id"]), {}),
                                          issues["byOutlet"].get(str(o["outlet_id"]), no_issues))}
                for o in outlets],
            "people": [{"actorId": a, "name": names[a], "role": people[a]["role"],
                        **by_channel(ch["byPerson"].get(a, {}), issues["byPerson"].get(a, no_issues))}
                       for a in visible if people[a]["role"] == "PC"],
        }

    if wanted("quality"):
        q = part("quality")

        def pages(source: dict[str, Any]) -> int:
            return int(sum(source.get("pagesUploaded", {}).get(d, 0) for d in iso))

        out["quality"] = {
            "outlets": [] if own else [
                {**place(o), "pagesUploaded": pages(data["by_outlet"].get(str(o["outlet_id"]), {})),
                 **q["byOutlet"].get(str(o["outlet_id"]), {})} for o in outlets],
            "people": [{"actorId": a, "name": names[a], "role": people[a]["role"],
                        "pagesUploaded": pages(values.get(a, {})), **q["byPerson"].get(a, {})}
                       for a in visible if people[a]["role"] == "PC"],
        }

    if wanted("tasks"):
        raised, backlog = part("tasks"), part("backlog")

        def open_now(counts: dict[str, list[int]]) -> dict[str, int]:
            return {"open": sum(counts.get("tasksOpen", [])), "overdue": sum(counts.get("tasksOverdue", []))}

        by_outlet = data["by_outlet"]
        out["tasks"] = {
            "outlets": [] if own else [
                {**place(o), "raised": [raised["raisedByOutlet"].get(str(o["outlet_id"]), {}).get(d, 0) for d in iso],
                 "closed": [by_outlet.get(str(o["outlet_id"]), {}).get("tasksClosed", {}).get(d, 0) for d in iso],
                 **open_now(backlog["byOutlet"].get(str(o["outlet_id"]), {}))} for o in outlets],
            "people": [
                {"actorId": a, "name": names[a], "role": people[a]["role"],
                 "raised": [raised["raisedByPerson"].get(a, {}).get(d, 0) for d in iso],
                 "closed": [values.get(a, {}).get("tasksClosed", {}).get(d, 0) for d in iso],
                 **open_now(backlog["byPerson"].get(a, {}))} for a in visible],
        }

    if wanted("efficiency"):
        eff = part("efficiency")
        table = []
        for a in visible:
            if people[a]["role"] != "PC":
                continue
            e = eff.get(a, {})
            v = values.get(a, {})
            pages = sum(v.get("pagesUploaded", {}).get(d, 0) for d in iso)
            rejected = sum(v.get("pagesRejected", {}).get(d, 0) for d in iso)
            journeys = sum(v.get("journeysUploaded", {}).get(d, 0) for d in iso)
            worked = sum(1 for d in iso if any(v.get(k, {}).get(d, 0) > 0 for k in ("filesUploaded", "tasksClosed")))
            started, closed = int(e.get("deliveryStarted", 0)), int(e.get("deliveryClosed", 0))
            due, in_time = int(e.get("tasksWithDue", 0)), int(e.get("tasksInTime", 0))
            table.append({
                "actorId": a, "name": names[a], "daysWorked": worked,
                "deliveryStarted": started, "deliveryClosed": closed,
                "closedShare": round(100 * closed / started) if started else None,
                "daysToClose": round(e["daysToCloseSum"] / closed, 1) if closed else None,
                "pagesUploaded": int(pages), "pagesRejected": int(rejected),
                "firstTimeRight": round(100 * (pages - rejected) / pages) if pages else None,
                "tasksWithDue": due, "tasksInTime": in_time,
                "tasksInTimeShare": round(100 * in_time / due) if due else None,
                "journeysPerDay": round(journeys / worked, 1) if worked else None,
            })
        out["efficiency"] = {"people": table}

    def duty_lines(actor: str) -> list[dict[str, Any]]:
        lines = []
        for schedule_id, by_day in duties.get(actor, {}).items():
            total = {"due": 0, "onTime": 0, "late": 0, "notDone": 0, "pending": 0}
            missed, late = [], []
            for d in sorted(by_day):
                cell = by_day[d]
                for k in total:
                    total[k] += cell[k]
                if cell["notDone"]:
                    missed.append(d)
                if cell["late"]:
                    late.append(d)
            if total["due"]:
                lines.append({"actorId": actor, "name": names[actor], "role": people[actor]["role"],
                              "scheduleId": schedule_id, **total, "missedDates": missed, "lateDates": late})
        return lines

    if wanted(DUTIES_ITEM):
        out["delinquency"] = {"lines": [line for a in visible for line in duty_lines(a)]}
    return out


def _caller_roles(connection: Connection, tenant_id: str, actor_id: str) -> list[str]:
    return [normalise_role(r[0]) or "" for r in connection.execute(text("""
        SELECT DISTINCT upper(business_role_code) FROM auditcore.business_assignments
        WHERE tenant_id = :t AND security_actor_id = :a AND assignment_status = 'ACTIVE'
          AND effective_from <= now() AND (effective_to IS NULL OR effective_to >= now())
    """), {"t": tenant_id, "a": actor_id})]


def _part(connection: Connection, name: str, *, tenant_id: str, first: date, last: date, tz: str, now: datetime,
          use_cache: bool) -> Any:
    """One part of the insights (backlog, efficiency, flow, tasks, channels, quality), read only when a
    section that needs it is asked for, and kept two minutes like the rest (product owner 13 Nov
    2026: "so many in one screen will kill the CPU and RAM, I don't want all to be shown in one")."""
    from audit_core import uc03_p2_team_insights as insights

    key = (tenant_id, f"{name}:{first.isoformat()}", last.isoformat())
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and _time.monotonic() - hit[0] < _CACHE_SECONDS:
                return hit[1]
    start, end = period_bounds(first, last, tz)
    zone = _zone(tz)
    if name == "deliveryState":
        data = insights._delivery_state(connection, tenant_id, now)
    elif name in ("backlog", "efficiency"):
        # Both start from the same two reads (every delivery file and the files closed): read once.
        state = _part(connection, "deliveryState", tenant_id=tenant_id, first=first, last=last, tz=tz, now=now,
                      use_cache=use_cache)
        if name == "backlog":
            data = insights.backlog(connection, tenant_id=tenant_id, zone=zone, now=now, state=state)
        else:
            data = insights.efficiency(connection, tenant_id=tenant_id, start=start, end=end, now=now, state=state)
    elif name == "flow":
        data = insights.flow(connection, tenant_id=tenant_id, first=first, last=last, start=start, end=end, tz=tz,
                             zone=zone, now=now)
    elif name == "tasks":
        data = insights.tasks(connection, tenant_id=tenant_id, start=start, end=end, tz=tz)
    elif name == "quality":
        data = insights.quality(connection, tenant_id=tenant_id, start=start, end=end)
    elif name == "channelIssues":
        data = insights.channel_issues(connection, tenant_id=tenant_id, start=start, end=end)
    else:
        data = insights.channels(connection, tenant_id=tenant_id, start=start, end=end, tz=tz)
    if use_cache:
        with _cache_lock:
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
    section: str | None = None,
) -> dict[str, Any]:
    """``section``: the tab that is open -- only its part is read and sent. None: everything."""
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

    # Whom the caller reads: their own row; or the people of their outlets whose role is below
    # their own (a TL: PCs and Reviewers; a PM: those and the TLs; an Executive: the PMs too).
    outlets: list[dict[str, Any]] = []
    caller_role: str | None = None  # an administrator: everything is shown
    rank = 0
    if own:
        visible = [actor_id] if actor_id in people else []
        caller_role = people.get(actor_id, {}).get("role") or "PC"
    else:
        wide, outlets = _scope(connection, tenant_id=tenant_id, actor_id=actor_id, admin=admin)
        outlet_ids = {str(o["outlet_id"]) for o in outlets}
        dealer_ids = {str(o["dealer_id"]) for o in outlets}
        held = _caller_roles(connection, tenant_id, actor_id)
        rank = 99 if admin else max([_ROLE_RANK.get(r, 0) for r in held] or [0])
        if not admin:
            caller_role = max(held, key=lambda r: _ROLE_RANK.get(r, 0)) if held else "TL"
        visible = [a for a, p in people.items()
                   if (wide or p["outlets"] & outlet_ids or p["dealers"] & dealer_ids)
                   and (_ROLE_RANK.get(p["role"], 0) < rank or a == actor_id)]

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

    hidden = hidden_items(connection, caller_role)
    if section == "dashboard":
        # The dashboard of the caller's role: one lean answer, shaped from the cached parts.
        def part(name: str) -> Any:
            return _part(connection, name, tenant_id=tenant_id, first=first, last=day, tz=tz, now=now,
                         use_cache=use_cache)

        view = "TEAM" if not own else "REVIEWER" if caller_role in ("REVIEWER", "CRM") else "PC"
        if view == "REVIEWER":  # a Reviewer's queue is the outlets they are mapped to
            _, outlets = _scope(connection, tenant_id=tenant_id, actor_id=actor_id, admin=False)
        return {
            "date": target, "first": first.isoformat(), "days": iso, "timezone": tz, "current": day == today,
            "asOfUtc": now.isoformat(), "own": own, "role": caller_role,
            "sections": [x for x in SECTIONS if x["key"] not in hidden],
            "dashboard": build_dashboard(part, data, view=view, actor_id=actor_id, visible=visible,
                                         names=data["names"], people=people, outlets=outlets, iso=iso,
                                         usual_days=usual_days, hidden=hidden, rank=rank, peers=peers,
                                         planned=[d for d in PLANNED_DUTIES if not any(
                                             s["title"].strip().lower() == d["title"].lower()
                                             for s in data["schedules"])]),
        }
    shown_keys = [k for k in _KEYS if k not in hidden]
    show_duties, show_peers = DUTIES_ITEM not in hidden, PEERS_ITEM not in hidden
    rows = []
    for actor in visible:
        person = people[actor]
        role = person["role"]
        peer = peers.get(role, {}) if show_peers else {}
        today_values = {k: values.get(actor, {}).get(k, {}).get(target, 0) for k in shown_keys}
        rows.append({
            "actorId": actor, "name": data["names"].get(actor), "role": role,
            "today": today_values,
            "usual": {k: usual(actor, k) for k in shown_keys},
            "series": {k: series(actor, k) for k in shown_keys},
            "standing": {k: standing(today_values[k], (peer.get("median") or {}).get(k), lower_is_better=k in _LOWER)
                         if active(actor, target) else None for k in shown_keys},
            "activeToday": active(actor, target),
            "dutiesToday": duty_totals(actor, target) if show_duties else {},
            "dutiesWindow": duty_totals(actor, None) if show_duties else {},
        })
    rows.sort(key=lambda r: (r["role"] or "~", (r["name"] or r["actorId"]).lower()))

    # ---- outlets and dealerships (not in a team member's own view)
    unit_measures = [m for m in UNIT_MEASURES if m.get("setting", m["key"]) not in hidden]
    unit_keys = [m["key"] for m in unit_measures]
    unit_lower = {m["key"] for m in unit_measures if m.get("lowerIsBetter")}
    by_outlet, outlet_duties = data["by_outlet"], data["outlet_duties"]

    def unit_figures(outlet_list: list[str]) -> dict[str, Any]:
        def cell(key: str, d: str) -> float:
            return sum(by_outlet.get(o, {}).get(key, {}).get(d, 0) for o in outlet_list)

        def duty(only: str | None) -> dict[str, int]:
            total = {"due": 0, "onTime": 0, "late": 0, "notDone": 0, "pending": 0}
            for o in outlet_list:
                for by_day in outlet_duties.get(o, {}).values():
                    for d, c in by_day.items():
                        if only is None or d == only:
                            for k in total:
                                total[k] += c[k]
            return total

        today_values = {k: cell(k, target) for k in unit_keys}
        return {
            "today": today_values,
            "usual": {k: (round(sum(cell(k, d) for d in usual_days) / len(usual_days), 1) if usual_days else None)
                      for k in unit_keys},
            "series": {k: [cell(k, d) for d in iso] for k in unit_keys},
            "activeToday": any(v > 0 for v in today_values.values()),
            "dutiesToday": duty(target) if show_duties else None,
            "dutiesWindow": duty(None) if show_duties else None,
        }

    def with_standing(units: list[dict[str, Any]]) -> None:
        present = [u for u in units if u["activeToday"]]
        medians = {k: (statistics.median(u["today"][k] for u in present) if len(present) > 1 else None)
                   for k in unit_keys}
        for u in units:
            u["standing"] = {k: (standing(u["today"][k], medians[k], lower_is_better=k in unit_lower)
                                 if show_peers and u["activeToday"] else None) for k in unit_keys}

    mapped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        row["outletIds"] = sorted(people[row["actorId"]]["outlets"])
        for o in row["outletIds"]:
            mapped[o].append(row["actorId"])
    outlet_units, dealer_units = [], []
    dealers_seen: dict[str, dict[str, Any]] = {}
    for o in outlets:
        oid, did = str(o["outlet_id"]), str(o["dealer_id"])
        outlet_units.append({"outletId": oid, "outletName": o["outlet_name"], "dealerId": did,
                             "dealerName": o["dealer_name"], "classification": o.get("outlet_classification"),
                             "people": len(mapped.get(oid, [])), **unit_figures([oid])})
        dealers_seen.setdefault(did, {"dealerId": did, "dealerName": o["dealer_name"], "outletIds": []})[
            "outletIds"].append(oid)
    for dealer in dealers_seen.values():
        dealer_units.append({**dealer, "outlets": len(dealer["outletIds"]),
                             "people": len({a for o in dealer["outletIds"] for a in mapped.get(o, [])}),
                             **unit_figures(dealer["outletIds"])})
    with_standing(outlet_units)
    with_standing(dealer_units)
    return {
        "date": target, "first": first.isoformat(), "days": iso, "timezone": tz, "current": day == today,
        "asOfUtc": now.isoformat(), "own": own,
        "measures": [dict(m) for m in MEASURES if m["key"] in shown_keys],
        "shows": {"duties": show_duties, "peers": show_peers},
        "schedules": data["schedules"] if show_duties else [],
        "plannedDuties": [d for d in PLANNED_DUTIES
                          if not any(s["title"].strip().lower() == d["title"].lower() for s in data["schedules"])
                          ] if show_duties else [],
        "peers": {role: {**p, "median": {k: p["median"][k] for k in shown_keys},
                         "usualMedian": {k: p["usualMedian"][k] for k in shown_keys}}
                  for role, p in peers.items()} if show_peers else {},
        "people": rows,
        **_sections(lambda name: _part(connection, name, tenant_id=tenant_id, first=first, last=day, tz=tz, now=now,
                                        use_cache=use_cache),
                    data, rows=rows, outlets=outlets, own=own, hidden=hidden, iso=iso, people=people, only=section),
        "unitMeasures": [{k: v for k, v in m.items() if k != "setting"} for m in unit_measures],
        "outlets": outlet_units,
        "dealers": sorted(dealer_units, key=lambda d: (d["dealerName"] or "").lower()),
        "notes": [
            ("Dealership and outlet figures are the same facts counted by the outlet the Journey or task belongs "
             "to, whoever did them; an outlet is compared with the other outlets that had anything that day, a "
             "dealership with the other dealerships."),
            ("Today's figure against the person's usual (their average over the earlier working days shown, "
            "Sundays left out) and against peers (the median, that day, of the people of the same role who "
            "did anything that day). Within a tenth of the median counts as 'with peers'."),
            ("Files and journeys uploaded count under the PC who uploaded them; tasks closed under whoever "
            "closed them; journeys closed are a PC's delivery files closed (all mandatory documents in and "
            "every document task closed) and the Journeys a Reviewer, TL or PM closed; reviews are Booking / "
            "Delivery documents-reviewed marks and a TL's or PM's accept or reject of a closure. Pages rejected are pages of that day's uploads "
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
    first: Annotated[date | None, Query(alias="from")] = None,
    section: Annotated[str | None, Query(pattern=r"^(dashboard|pending|flow|uploads|quality|tasks|trend|efficiency|duties)$")] = None,
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
    if first is not None:
        # A date range (product owner 13 Nov 2026: "first it should have date range"): from .. date.
        if first > day:
            raise HTTPException(status_code=422, detail="'from' is after 'date'.")
        days = min(MAX_DAYS, (day - first).days + 1)
    return build_performance(connection, tenant_id=tenant_id, actor_id=human_principal.subject, day=day,
                             days=days, admin=admin, own=not team, section=section)


# --------------------------------------------------------------------------- SuperAdmin settings

admin_router = APIRouter(tags=["performance-settings"])


class PerformanceSettingsCommand(BaseModel):
    shown: dict[str, bool]


def _settings(connection: Connection) -> dict[str, Any]:
    rows = {str(r["item_key"]): bool(r["shown"]) for r in connection.execute(text(
        "SELECT item_key, shown FROM platform_ops.performance_settings")).mappings()}
    return {"roles": list(SETTING_ROLES), "items": [
        {**item, "shown": {role: rows.get(f"{role}:{item['key']}", True) for role in SETTING_ROLES}}
        for item in SETTING_ITEMS]}


@admin_router.get("/v1/admin/performance-settings")
def get_performance_settings(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    set_platform_super_admin_context(connection)
    return _settings(connection)


@admin_router.put("/v1/admin/performance-settings")
def set_performance_settings(
    command: PerformanceSettingsCommand,
    admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Show or hide an item of the Performance view for a role ("TL:pending": false), for every
    Project. Takes effect at once."""
    known = {f"{role}:{item['key']}" for role in SETTING_ROLES for item in SETTING_ITEMS}
    unknown = sorted(set(command.shown) - known)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown item: {', '.join(unknown)}.")
    set_platform_super_admin_context(connection)
    for key, shown in command.shown.items():
        connection.execute(text("""
            INSERT INTO platform_ops.performance_settings (item_key, shown, updated_by)
            VALUES (:k, :s, :by)
            ON CONFLICT (item_key) DO UPDATE SET shown = EXCLUDED.shown, updated_by = EXCLUDED.updated_by,
              updated_at_utc = now()
        """), {"k": key, "s": shown, "by": admin.user_id})
    return _settings(connection)
