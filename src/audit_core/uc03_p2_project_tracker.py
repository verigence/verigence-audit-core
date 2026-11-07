"""Project Tracker: the situation of the whole Project for a day, a week or a calendar month
(product owner, 2026-11-07).

"It is to be Project Tracker specifically to see the situation around the complete
project, gate pass, files upload, files closed, tasks worked, each dealership wise,
outlet wise, PC wise, and it will [in] future [have] daily weekly task status as well,
like some reconciliations DMS, payments, etc."

``GET /p2/v1/tenants/{t}/reports/project-tracker?period=DAY|WEEK|MONTH&date=YYYY-MM-DD``
for a Team Lead, PM or Executive (an administrator too), over the outlets they are
mapped to (a mapping with no dealer is the whole Project). The period is the day, the
week (Monday to Sunday) containing ``date``, or the calendar month containing it, in
the Project's timezone (India time). Every figure is read from what Audit Core already
stores -- nothing is written -- and is returned for the Project, each dealership, each
outlet and each person (the PC; a task not tied to a PC sits on the outlet's "not linked
to a PC" row, so the rows add up to the outlet):

Gate pass (the gate register, uc03_p2_gate_pass; the same rules as My Tracker and the
consolidated view)
    due             gate register days of the period up to today x outlets with a PC
                    mapped (Sunday is off, Monday's covers it; satellites from 3 Nov 2026;
                    never before the Project's Daily Gate Pass schedule started)
    onTime / late   registers on file, uploaded by / after their Daily Gate Pass task's due
                    time (23:59 IST of the next gate pass day); under the PC who uploaded
    missing         no register and its due time has passed
    notYetDue       no register, still within its due time (today's, yesterday's)
    countsPending   a register whose counts the PC has not entered
    onRegister, intimated, notNotified, uploadedInTool, pendingWithPc, stockTransfers
                    the counts the PCs entered and the tool's own count of delivery files
                    (figures only: the Team Lead's "Delivery not notified" task is suspended)

Files uploaded (p2_upload_batches created in the period; a duplicate cancelled on
arrival is not an upload), under the PC who uploaded
    files, pages, documents (the units the pages became), journeys uploaded to, of which
    deliveries (a delivery page among those uploads) and bookings (none); newJourneys:
    Journeys started in the period, under the PC who started them

Files closed, under the Journey's PC
    deliveryFiles   the product owner's "delivery file closed" rule (2026-11-02), exactly as
                    the Daily processing report and My Tracker count it
                    (uc03_p2_daily_report.delivery_files_closed), on the day it last closed
    reviewer        Journeys a Reviewer closed in the period (p2_journey_closures)

Tasks (p2_tasks: Journey tasks and Task Manager tasks apart), per assigned role too
    raised          created in the period
    closed          closed in the period (action completed, or verified), split by who
                    closed it: the role of the person whose action closed it (or on whose
                    behalf the system closed it: the gate pass task closes when the PC
                    finishes the register), else the system (a machine re-check that found
                    the problem gone)
    open, overdue   open at the end of the period (now, for the current period), and of
                    those past their due time
    worked          tasks a person acted on in the period (a task counts once), by the
                    actor's role

Recurring tasks (p2_task_schedules / their occurrences in p2_tasks), one entry per
schedule -- the Daily Gate Pass today; any schedule an Executive adds (a DMS or payment
reconciliation) appears with no new code
    due (occurrences in the period, cancelled left out) = onTime + late + incomplete +
    overdue + pending
"""
from __future__ import annotations

import calendar as _calendar
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.task_manager import GATE_PASS_SYSTEM_KIND, known_names, normalise_role
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_daily_report import _require_report_role, delivery_files_closed
from audit_core.uc03_p2_gate_pass import scope_outlets
from audit_core.uc03_p2_my_work import SATELLITE_REGISTER_FROM, gate_register

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-project-tracker"])

_READ_PERMISSION = "audit.journey.read"
_ADMIN_ROLES = ("SUPER_ADMIN", "TENANT_ADMIN")
_FALLBACK_TIMEZONE = "Asia/Kolkata"
PERIODS = ("DAY", "WEEK", "MONTH")

# Roles a task is assigned to (anything else is shown as OTHER).
TASK_ROLES = ("PC", "TL", "PM", "EXECUTIVE", "REVIEWER", "CRM")
_LEAD_ROLES = frozenset({"TL", "PM", "EXECUTIVE"})
_ENDED = ("CANCELLED", "DEAD_LETTER", "FAILED")

# What a person does on a task (as the Daily Processing report and My Tracker count work)
# and the actions by which a person closes one.
_WORK_EVENTS = (
    "UPLOAD_DOCUMENT", "REUPLOAD_DOCUMENT", "REVIEW_DOCUMENT", "CORRECT_EXTRACTED_FIELD", "ADD_EVIDENCE",
    "ADD_COMMENT", "PROVIDE_FEEDBACK", "REQUEST_CLARIFICATION", "COMPLETE_ACTION", "APPROVE_CORRECTION",
    "REJECT_CORRECTION", "ACCEPT", "REJECT", "ACCEPT_EXCEPTION", "PROVIDE_VEHICLE_ID", "CONFIRM_BREACH",
    "MARK_FALSE_POSITIVE", "CLOSE_TASK", "COMPLETED", "VERDICT", "STARTED", "ACCEPTED",
    "CLOSED_BY_REVIEWER_CLOSURE",
)
_CLOSING_EVENTS = frozenset({
    "COMPLETE_ACTION", "CLOSE_TASK", "ACCEPT", "ACCEPT_EXCEPTION", "CONFIRM_BREACH", "MARK_FALSE_POSITIVE",
    "APPROVE_CORRECTION", "REJECT_CORRECTION", "VERDICT", "COMPLETED", "ACCEPTED", "CLOSED_BY_REVIEWER_CLOSURE",
})

PROJECT_WIDE = "PROJECT"  # the group of Task Manager tasks with no outlet (Project-wide)


# --------------------------------------------------------------------------- period

def period_range(kind: str, day: date) -> tuple[date, date]:
    """The first and last day of the period: the day, the week (Monday to Sunday) containing
    it, or its calendar month."""
    kind = kind.upper()
    if kind == "DAY":
        return day, day
    if kind == "WEEK":
        monday = day - timedelta(days=day.weekday())
        return monday, monday + timedelta(days=6)
    if kind == "MONTH":
        return day.replace(day=1), day.replace(day=_calendar.monthrange(day.year, day.month)[1])
    raise ValueError(f"Unknown period {kind!r}.")


def period_label(kind: str, first: date, last: date) -> str:
    kind = kind.upper()
    if kind == "DAY":
        return f"{first:%a} {first.day} {first:%b} {first.year}"
    if kind == "WEEK":
        start = f"{first.day} {first:%b}" + ("" if first.year == last.year else f" {first.year}")
        return f"{start} – {last.day} {last:%b} {last.year}"
    return f"{first:%B %Y}"


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or _FALLBACK_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(_FALLBACK_TIMEZONE)


def period_bounds(first: date, last: date, timezone_name: str | None) -> tuple[datetime, datetime]:
    """[start, end) in UTC: midnight of the first day to midnight after the last, in the
    Project's timezone."""
    zone = _zone(timezone_name)
    start = datetime.combine(first, time(0), zone).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time(0), zone).astimezone(UTC)
    return start, end


# --------------------------------------------------------------------------- figures

def _blank() -> dict[str, Any]:
    def task_section() -> dict[str, int]:
        return {"raised": 0, "closed": 0, "closedByPc": 0, "closedByLead": 0, "closedByReviewer": 0,
                "closedByOther": 0, "closedBySystem": 0, "open": 0, "overdue": 0}

    return {
        "gatePass": {"due": 0, "onTime": 0, "late": 0, "missing": 0, "notYetDue": 0, "countsPending": 0,
                     "onRegister": 0, "intimated": 0, "notNotified": 0, "uploadedInTool": 0, "pendingWithPc": 0,
                     "stockTransfers": 0},
        "uploads": {"files": 0, "pages": 0, "documents": 0, "journeys": set(), "deliveries": set(),
                    "newJourneys": 0},
        "closed": {"deliveryFiles": 0, "reviewer": 0},
        "tasks": {
            "journey": task_section(), "taskManager": task_section(),
            "byRole": defaultdict(lambda: {"raised": 0, "closed": 0, "open": 0, "overdue": 0}),
            "worked": {"total": set(), "byPc": set(), "byLead": set(), "byReviewer": set(), "byOther": set()},
        },
        "recurring": defaultdict(lambda: {"due": 0, "onTime": 0, "late": 0, "incomplete": 0, "overdue": 0,
                                          "pending": 0}),
    }


def _finish(figures: dict[str, Any]) -> dict[str, Any]:
    """Sets become counts; bookings are the Journeys uploaded to with no delivery page."""
    uploads = figures["uploads"]
    journeys, deliveries = uploads.pop("journeys"), uploads.pop("deliveries")
    uploads["journeys"], uploads["deliveries"] = len(journeys), len(deliveries)
    uploads["bookings"] = len(journeys - deliveries)
    tasks = figures["tasks"]
    tasks["worked"] = {key: len(value) for key, value in tasks["worked"].items()}
    tasks["byRole"] = {role: dict(v) for role, v in sorted(tasks["byRole"].items())}
    journey, manager = tasks["journey"], tasks["taskManager"]
    tasks["all"] = {key: journey[key] + manager[key] for key in journey}
    figures["recurring"] = {key: dict(v) for key, v in figures["recurring"].items()}
    return figures


class _Tree:
    """The Project, its dealerships, outlets and people; every fact is added at all four."""

    def __init__(self, outlets: list[dict[str, Any]], names: dict[str, str], roles: dict[str, str]):
        self.names, self.roles = names, roles
        self.total = _blank()
        self.dealers: dict[str, dict[str, Any]] = {}
        self.outlets: dict[str, dict[str, Any]] = {}
        self._dealer_of: dict[str, dict[str, Any]] = {}
        for o in outlets:
            self._outlet(str(o["outlet_id"]), o)

    def _dealer(self, dealer_id: str | None, name: str | None) -> dict[str, Any]:
        key = dealer_id or PROJECT_WIDE
        if key not in self.dealers:
            self.dealers[key] = {"dealerId": dealer_id, "dealerName": name or "Project-wide (no outlet)",
                                 "figures": _blank(), "outlets": []}
        return self.dealers[key]

    def _outlet(self, outlet_id: str, o: dict[str, Any] | None) -> dict[str, Any]:
        if outlet_id not in self.outlets:
            dealer = self._dealer(str(o["dealer_id"]) if o else None, o["dealer_name"] if o else None)
            node = {"outletId": None if outlet_id == PROJECT_WIDE else outlet_id,
                    "outletName": o["outlet_name"] if o else "Tasks not tied to an outlet",
                    "classification": o.get("outlet_classification") if o else None,
                    "figures": _blank(), "people": {}}
            self.outlets[outlet_id] = node
            self._dealer_of[outlet_id] = dealer
            dealer["outlets"].append(node)
        return self.outlets[outlet_id]

    def has_outlet(self, outlet_id: str) -> bool:
        return outlet_id in self.outlets

    def person(self, outlet_id: str, actor_id: str | None) -> dict[str, Any]:
        people = self.outlets[outlet_id]["people"]
        key = actor_id or ""
        if key not in people:
            people[key] = {"actorId": actor_id, "name": self.names.get(key) if actor_id else None,
                           "role": self.roles.get(key) if actor_id else None, "figures": _blank()}
        return people[key]

    def nodes(self, outlet_id: str | None, actor_id: str | None) -> list[dict[str, Any]]:
        key = outlet_id or PROJECT_WIDE
        if key not in self.outlets:
            self._outlet(key, None)
        return [self.total, self._dealer_of[key]["figures"], self.outlets[key]["figures"],
                self.person(key, actor_id)["figures"]]

    def result(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dealers = []
        for dealer in self.dealers.values():
            outlets = []
            for outlet in dealer["outlets"]:
                people = sorted(outlet["people"].values(),
                                key=lambda p: (p["actorId"] is None, (p["name"] or p["actorId"] or "").lower()))
                outlets.append({**{k: v for k, v in outlet.items() if k not in ("figures", "people")},
                                **_finish(outlet["figures"]),
                                "people": [{**{k: v for k, v in p.items() if k != "figures"}, **_finish(p["figures"])}
                                           for p in people]})
            dealers.append({"dealerId": dealer["dealerId"], "dealerName": dealer["dealerName"],
                            **_finish(dealer["figures"]), "outlets": outlets})
        # Dealerships by name; the Project-wide group last.
        dealers.sort(key=lambda d: (d["dealerId"] is None, (d["dealerName"] or "").lower()))
        return _finish(self.total), dealers


def _bump(nodes: list[dict[str, Any]], section: str, key: str, n: int = 1) -> None:
    for figures in nodes:
        figures[section][key] += n


def _member(nodes: list[dict[str, Any]], section: str, key: str, member: Any) -> None:
    for figures in nodes:
        figures[section][key].add(member)


# --------------------------------------------------------------------------- reads

def _scope(connection: Connection, *, tenant_id: str, actor_id: str, admin: bool) -> tuple[bool, list[dict[str, Any]]]:
    """(Project-wide, outlets) the caller reads: an administrator, or a mapping with no dealer,
    is the whole Project."""
    if admin:
        rows = connection.execute(
            text("""
                SELECT o.dealer_id, d.dealer_name, o.outlet_id, o.outlet_name, o.outlet_classification
                FROM auditcore.dealer_outlets o
                JOIN auditcore.dealers d ON d.tenant_id = o.tenant_id AND d.dealer_id = o.dealer_id
                WHERE o.tenant_id = :t
                ORDER BY lower(d.dealer_name), (o.outlet_classification = 'ONSITE') DESC, lower(o.outlet_name)
            """),
            {"t": tenant_id},
        ).mappings().all()
        return True, [dict(r) for r in rows]
    wide = connection.execute(
        text("""
            SELECT 1 FROM auditcore.business_assignments
            WHERE tenant_id = :t AND security_actor_id = :a AND assignment_status = 'ACTIVE'
              AND effective_from <= now() AND (effective_to IS NULL OR effective_to >= now())
              AND dealer_id IS NULL
            LIMIT 1
        """),
        {"t": tenant_id, "a": actor_id},
    ).scalar_one_or_none() is not None
    return wide, scope_outlets(connection, tenant_id, actor_id)


def _people(connection: Connection, tenant_id: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Each person's operating role, and the PCs mapped to each outlet now."""
    roles: dict[str, str] = {}
    pcs: dict[str, list[str]] = defaultdict(list)
    for r in connection.execute(
        text("""
            SELECT security_actor_id, upper(business_role_code) AS role, outlet_id
            FROM auditcore.business_assignments
            WHERE tenant_id = :t AND assignment_status = 'ACTIVE'
              AND effective_from <= now() AND (effective_to IS NULL OR effective_to >= now())
        """),
        {"t": tenant_id},
    ).mappings():
        role = normalise_role(r["role"]) or ""
        actor = str(r["security_actor_id"])
        # One operating role per person and Project; a PC mapping wins for naming the row.
        if actor not in roles or role == "PC":
            roles[actor] = role
        if role == "PC" and r["outlet_id"] is not None and actor not in pcs[str(r["outlet_id"])]:
            pcs[str(r["outlet_id"])].append(actor)
    return roles, pcs


def _gate_pass_start(connection: Connection, tenant_id: str) -> date | None:
    return connection.execute(
        text("SELECT starts_on FROM auditcore.p2_task_schedules WHERE tenant_id = :t AND system_kind = :k"),
        {"t": tenant_id, "k": GATE_PASS_SYSTEM_KIND},
    ).scalar_one_or_none()


def _add_gate_pass(connection: Connection, tree: _Tree, *, tenant_id: str, outlets: list[dict[str, Any]],
                   pcs: dict[str, list[str]], first: date, last: date, today: date, now: datetime) -> None:
    starts = _gate_pass_start(connection, tenant_id)
    start, end = max(first, starts) if starts else None, min(last, today)
    with_pc = [o for o in outlets if pcs.get(str(o["outlet_id"]))]
    if start is None or start > end or not with_pc:
        return
    register = gate_register(connection, tenant_id, with_pc, start, end, now=now)
    for oid, days in register.items():
        for d in days:
            if d["gatePassId"] is None:
                nodes = tree.nodes(oid, None)
                _bump(nodes, "gatePass", "due")
                _bump(nodes, "gatePass", "missing" if d["status"] == "MISSED" else "notYetDue")
                continue
            nodes = tree.nodes(oid, d["pcActorId"])
            _bump(nodes, "gatePass", "due")
            _bump(nodes, "gatePass", "late" if d["uploadedLate"] else "onTime")
            if d["status"] == "COUNTS_PENDING":
                _bump(nodes, "gatePass", "countsPending")
                continue
            for key, value in (("onRegister", d["total"]), ("intimated", d["intimated"]),
                               ("notNotified", d["notIntimated"]), ("uploadedInTool", d["uploadedInTool"]),
                               ("pendingWithPc", d["pendingWithPc"]), ("stockTransfers", d["stockTransfers"])):
                _bump(nodes, "gatePass", key, int(value or 0))


def _add_uploads(connection: Connection, tree: _Tree, *, tenant_id: str, start: datetime, end: datetime) -> None:
    rows = connection.execute(
        text("""
            SELECT ub.batch_id, ub.journey_id, j.outlet_id, ub.uploaded_by_actor_id AS actor_id,
                   COALESCE(ub.page_count, 0) AS pages,
                   COUNT(q.queue_id) FILTER (WHERE q.queue_status NOT IN ('MERGED', 'CANCELLED')) AS documents,
                   COALESCE(bool_or(q.business_stage = 'DELIVERY'), false) AS delivery
            FROM auditcore.p2_upload_batches ub
            JOIN auditcore.journeys j ON j.tenant_id = ub.tenant_id AND j.journey_id = ub.journey_id
            LEFT JOIN auditcore.p2_document_queue q ON q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
            WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
              AND ub.batch_status <> 'AWAITING_UPLOAD'
              AND NOT (ub.batch_status = 'CANCELLED' AND COALESCE(ub.page_count, 0) = 0)
            GROUP BY ub.batch_id, ub.journey_id, j.outlet_id, ub.uploaded_by_actor_id, ub.page_count
        """),
        {"t": tenant_id, "s": start, "e": end},
    ).mappings().all()
    for r in rows:
        oid = str(r["outlet_id"])
        if not tree.has_outlet(oid):
            continue
        nodes = tree.nodes(oid, r["actor_id"])
        _bump(nodes, "uploads", "files")
        _bump(nodes, "uploads", "pages", int(r["pages"]))
        _bump(nodes, "uploads", "documents", int(r["documents"]))
        _member(nodes, "uploads", "journeys", str(r["journey_id"]))
        if r["delivery"]:
            _member(nodes, "uploads", "deliveries", str(r["journey_id"]))
    for r in connection.execute(
        text("""
            SELECT j.outlet_id, j.created_by_actor_id FROM auditcore.journeys j
            WHERE j.tenant_id = :t AND j.created_at_utc >= :s AND j.created_at_utc < :e
        """),
        {"t": tenant_id, "s": start, "e": end},
    ).mappings():
        oid = str(r["outlet_id"])
        if tree.has_outlet(oid):
            _bump(tree.nodes(oid, r["created_by_actor_id"]), "uploads", "newJourneys")


def _add_closed(connection: Connection, tree: _Tree, *, tenant_id: str, actor_id: str, start: datetime,
                end: datetime, wide: bool) -> None:
    files = delivery_files_closed(connection, tenant_id=tenant_id, actor_id=actor_id, start=start, end=end,
                                  names={}, whole_project=wide)["files"]
    for f in files:
        oid = f.get("outletId")
        if oid and tree.has_outlet(oid):
            _bump(tree.nodes(oid, f["pcActorId"]), "closed", "deliveryFiles")
    for r in connection.execute(
        text("""
            SELECT DISTINCT c.journey_id, j.outlet_id, j.created_by_actor_id
            FROM auditcore.p2_journey_closures c
            JOIN auditcore.journeys j ON j.tenant_id = c.tenant_id AND j.journey_id = c.journey_id
            WHERE c.tenant_id = :t AND c.closed_at_utc >= :s AND c.closed_at_utc < :e
        """),
        {"t": tenant_id, "s": start, "e": end},
    ).mappings():
        oid = str(r["outlet_id"])
        if tree.has_outlet(oid):
            _bump(tree.nodes(oid, r["created_by_actor_id"]), "closed", "reviewer")


def _closer_key(role: str | None) -> str:
    role = normalise_role(role) or ""
    if role == "PC":
        return "Pc"
    if role in _LEAD_ROLES:
        return "Lead"
    if role == "REVIEWER":
        return "Reviewer"
    return "Other"


_TASKS_SQL = """
    SELECT t.task_id, t.scope_kind, t.task_type, t.schedule_id, t.occurrence_date, t.task_status,
           upper(COALESCE(t.assigned_role_code, '')) AS assigned_role, t.assigned_actor_id,
           t.completed_by_actor_id, t.created_at_utc, t.updated_at_utc, t.due_at_utc, t.missed_at_utc,
           t.completion_result->>'outcome' AS outcome,
           COALESCE(t.action_completed_at_utc,
                    CASE WHEN t.task_status = 'VERIFIED_COMPLETE' THEN COALESCE(t.verified_at_utc, t.updated_at_utc) END)
             AS done_at,
           COALESCE(t.outlet_id, j.outlet_id) AS outlet_id, j.created_by_actor_id AS journey_pc
    FROM auditcore.p2_tasks t
    LEFT JOIN auditcore.journeys j ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
    WHERE t.tenant_id = :t AND t.created_at_utc < :e
      AND (t.scope_kind <> 'JOURNEY' OR upper(t.assigned_role_code) = ANY(:roles))
      AND (t.created_at_utc >= :s OR t.updated_at_utc >= :s
           OR t.action_completed_at_utc >= :s OR t.verified_at_utc >= :s
           OR t.task_status NOT IN ('VERIFIED_COMPLETE', 'CANCELLED', 'FAILED', 'DEAD_LETTER')
           OR (t.schedule_id IS NOT NULL AND t.occurrence_date BETWEEN :first AND :last))
"""


def _person_for(task: dict[str, Any]) -> str | None:
    """Whose row a task counts on: the person it is assigned to; a PC task with no person, the
    Journey's PC (as the Daily Status counts it); a team task on a Journey, the Journey's PC
    (the PC whose file it is); otherwise nobody (the outlet's "not linked to a PC" row)."""
    if task["assigned_actor_id"]:
        return str(task["assigned_actor_id"])
    if task["journey_pc"]:
        return str(task["journey_pc"])
    if task["assigned_role"] == "PC" and task["completed_by_actor_id"]:
        return str(task["completed_by_actor_id"])
    return None


def _add_tasks(connection: Connection, tree: _Tree, *, tenant_id: str, start: datetime, end: datetime,
               first: date, last: date, now: datetime, wide: bool) -> None:
    cut = min(end, now)
    tasks = {str(r["task_id"]): dict(r) for r in connection.execute(
        text(_TASKS_SQL),
        {"t": tenant_id, "s": start, "e": end, "first": first, "last": last, "roles": list(TASK_ROLES)},
    ).mappings()}
    events = connection.execute(
        text("""
            SELECT te.task_id, te.actor_id, upper(COALESCE(te.actor_role_code, '')) AS role, te.event_type,
                   te.created_at_utc
            FROM auditcore.p2_task_events te
            WHERE te.tenant_id = :t AND te.created_at_utc >= :s AND te.created_at_utc < :e
              AND te.actor_id IS NOT NULL AND upper(te.actor_id) <> 'SYSTEM' AND te.event_type = ANY(:work)
            ORDER BY te.created_at_utc
        """),
        {"t": tenant_id, "s": start, "e": end, "work": list(_WORK_EVENTS)},
    ).mappings().all()
    closer: dict[str, str] = {}
    for ev in events:
        if ev["event_type"] in _CLOSING_EVENTS:
            closer[str(ev["task_id"])] = ev["role"]  # the latest closing action in the period

    def where(task: dict[str, Any]) -> str | None:
        """The outlet the task counts under, or None when outside the caller's scope."""
        if task["outlet_id"] is None:
            return PROJECT_WIDE if wide else None
        oid = str(task["outlet_id"])
        return oid if tree.has_outlet(oid) else None

    for task_id, task in tasks.items():
        oid = where(task)
        if oid is None:
            continue
        nodes = tree.nodes(None if oid == PROJECT_WIDE else oid, _person_for(task))
        section = "journey" if task["scope_kind"] == "JOURNEY" else "taskManager"
        role = normalise_role(task["assigned_role"]) or "OTHER"
        role = role if role in TASK_ROLES else "OTHER"
        status, done = task["task_status"], task["done_at"]
        created = task["created_at_utc"]
        counts: list[str] = []
        if start <= created < end:
            counts.append("raised")
        if done is not None and start <= done < end:
            counts.append("closed")
            human = closer.get(task_id)
            # A task the system closes on a person's behalf (the gate pass task when the PC
            # finishes the register) is that person's closing.
            completer = str(task["completed_by_actor_id"] or "")
            if human is None and completer and completer.upper() != "SYSTEM":
                human = tree.roles.get(completer) or "OTHER"
            counts.append("closedBy" + (_closer_key(human) if human else "System"))
        ended_before_cut = status in _ENDED and task["updated_at_utc"] < cut
        if created < cut and not (done is not None and done < cut) and not ended_before_cut:
            counts.append("open")
            if task["due_at_utc"] is not None and task["due_at_utc"] < cut:
                counts.append("overdue")
        for key in counts:
            for figures in nodes:
                figures["tasks"][section][key] += 1
                if key in ("raised", "closed", "open", "overdue"):
                    figures["tasks"]["byRole"][role][key] += 1
        if task["schedule_id"] is not None and task["occurrence_date"] is not None \
                and first <= task["occurrence_date"] <= last and status not in ("CANCELLED", "DEAD_LETTER"):
            _add_occurrence(nodes, task, now)

    for ev in events:
        task = tasks.get(str(ev["task_id"]))
        if task is None:
            continue
        oid = where(task)
        if oid is None:
            continue
        actor_role = normalise_role(ev["role"])
        person = str(ev["actor_id"]) if actor_role == "PC" else _person_for(task)
        nodes = tree.nodes(None if oid == PROJECT_WIDE else oid, person)
        for figures in nodes:
            worked = figures["tasks"]["worked"]
            worked["total"].add(str(ev["task_id"]))
            worked["by" + _closer_key(actor_role)].add(str(ev["task_id"]))


def _add_occurrence(nodes: list[dict[str, Any]], task: dict[str, Any], now: datetime) -> None:
    """One occurrence of a recurring task: done on time, done late, incomplete (the gate pass
    not finished by its deadline), overdue (open past its due time) or pending."""
    done, due = task["done_at"], task["due_at_utc"]
    if done is not None:
        late = task["outcome"] == "DONE_LATE" or (due is not None and done > due)
        state = "late" if late else "onTime"
    elif task["task_status"] == "FAILED":
        state = "incomplete"
    elif due is not None and due < now:
        state = "overdue"
    else:
        state = "pending"
    key = str(task["schedule_id"])
    for figures in nodes:
        figures["recurring"][key]["due"] += 1
        figures["recurring"][key][state] += 1


def _schedules(connection: Connection, tenant_id: str, keys: set[str]) -> list[dict[str, Any]]:
    """The recurring task schedules shown as columns: every active one, and any other with an
    occurrence in the period. The Daily Gate Pass first."""
    rows = connection.execute(
        text("""
            SELECT schedule_id, system_kind, title, frequency, category, schedule_status, assigned_role_code
            FROM auditcore.p2_task_schedules
            WHERE tenant_id = :t AND (schedule_status = 'ACTIVE' OR schedule_id::text = ANY(:keys))
            ORDER BY (system_kind = :gate) DESC NULLS LAST, lower(title)
        """),
        {"t": tenant_id, "keys": sorted(keys), "gate": GATE_PASS_SYSTEM_KIND},
    ).mappings().all()
    return [{"scheduleId": str(r["schedule_id"]), "systemKind": r["system_kind"], "title": r["title"],
             "frequency": r["frequency"], "category": r["category"], "status": r["schedule_status"],
             "assignedRole": normalise_role(r["assigned_role_code"])} for r in rows]


# --------------------------------------------------------------------------- the report

def build_project_tracker(
    connection: Connection, *, tenant_id: str, actor_id: str, period: str, day: date, admin: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    kind = period.upper()
    timezone_name = connection.execute(
        text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant_id},
    ).scalar_one_or_none() or _FALLBACK_TIMEZONE
    today = now.astimezone(_zone(timezone_name)).date()
    first, last = period_range(kind, day)
    if first > today:
        raise HTTPException(status_code=422, detail="That period has not started yet.")
    start, end = period_bounds(first, last, timezone_name)
    wide, outlets = _scope(connection, tenant_id=tenant_id, actor_id=actor_id, admin=admin)
    roles, pcs = _people(connection, tenant_id)
    tree = _Tree(outlets, known_names(connection, tenant_id=tenant_id), roles)
    # Every PC mapped to an outlet has a row, also with nothing done in the period.
    for o in outlets:
        for pc in pcs.get(str(o["outlet_id"]), []):
            tree.person(str(o["outlet_id"]), pc)

    _add_gate_pass(connection, tree, tenant_id=tenant_id, outlets=outlets, pcs=pcs, first=first, last=last,
                   today=today, now=now)
    _add_uploads(connection, tree, tenant_id=tenant_id, start=start, end=end)
    _add_closed(connection, tree, tenant_id=tenant_id, actor_id=actor_id, start=start, end=end, wide=wide)
    _add_tasks(connection, tree, tenant_id=tenant_id, start=start, end=end, first=first, last=last, now=now,
               wide=wide)
    totals, dealers = tree.result()
    schedules = _schedules(connection, tenant_id, set(totals["recurring"]))
    return {
        "period": {
            "kind": kind, "date": day.isoformat(), "first": first.isoformat(), "last": last.isoformat(),
            "label": period_label(kind, first, last), "timezone": timezone_name,
            "startUtc": start.isoformat(), "endUtc": end.isoformat(), "asOfUtc": min(end, now).isoformat(),
            "current": first <= today <= last,
        },
        "scope": {"projectWide": wide, "outlets": len(outlets), "dealers": len({str(o["dealer_id"]) for o in outlets})},
        "schedules": schedules,
        "totals": totals,
        "dealers": dealers,
        "satelliteRegistersFrom": SATELLITE_REGISTER_FROM.isoformat(),
        "notes": [
            ("Gate pass: registers due are the gate register days of the period up to today (Sunday is off; "
             "Monday's covers it) for every outlet with a PC mapped, from the day the Daily Gate Pass started "
             "(satellite outlets from 3 Nov 2026). On time / late: uploaded by / after 23:59 of the next gate "
             "pass day, under the PC who uploaded it. Missing: none on file and that time has passed."),
            ("Gate pass counts are what the PCs entered; 'not notified' are deliveries the PC said were not "
             "intimated to them (figures only: the Team Lead's task for them is suspended); 'in tool' are the "
             "delivery files whose vehicle gate pass is dated that day."),
            ("Files uploaded: uploads made in the period under the PC who uploaded them (a duplicate dropped "
             "on arrival is not counted). A Journey counts as a delivery when a delivery page is among those "
             "uploads, otherwise as a booking; a Journey uploaded to by two PCs counts once on the outlet."),
            ("Delivery files closed: all mandatory delivery documents in (or the PC closed their 'Upload the "
             "...' task) and every document task closed, counted on the day the last of them closed, under "
             "the Journey's PC (the Daily processing report's rule). Closed by Reviewer: Journeys a Reviewer "
             "closed in the period."),
            ("Tasks: raised in the period; closed in the period, by the role of the person whose action "
             "closed it (System: closed by a machine re-check); open at the end of the period (now for the "
             "current period) and of those overdue. A team task on a Journey counts on the row of the "
             "Journey's PC; a task not tied to a PC counts on the outlet's 'not linked to a PC' row."),
            ("Open at the end of a past period is read from the tasks as they are now: a task closed later "
             "counts as open then; one reopened later counts as closed when it was closed."),
            ("Recurring tasks: every occurrence of each schedule in the period (cancelled ones left out) -- "
             "done on time, done late, incomplete, overdue (open past its due time) or not yet due. A new "
             "schedule (a DMS or payment reconciliation) appears here on its own."),
        ],
    }


@router.get("/reports/project-tracker")
def get_project_tracker(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    period: Annotated[str, Query(pattern=r"(?i)^(day|week|month)$")] = "DAY",
    day: Annotated[date | None, Query(alias="date")] = None,
) -> dict[str, Any]:
    """The Project Tracker (product owner 2026-11-07): gate pass, files uploaded, files closed,
    tasks and recurring tasks for a day, a week (Monday to Sunday) or a calendar month, per
    dealership, outlet and PC. Team Lead, PM, Executive (and an administrator)."""
    access = authorize_p2(
        connection, tenant_id=tenant_id, journey_id=None, human_principal=human_principal,
        authorization_client=authorization_client, permission_key=_READ_PERMISSION,
    )
    _require_report_role(connection, tenant_id=tenant_id, actor_id=human_principal.subject,
                         functional_role=access.functional_role)
    admin = (access.functional_role or "").upper() in _ADMIN_ROLES
    if day is None:
        timezone_name = connection.execute(
            text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant_id},
        ).scalar_one_or_none()
        day = datetime.now(UTC).astimezone(_zone(timezone_name)).date()
    return build_project_tracker(connection, tenant_id=tenant_id, actor_id=human_principal.subject,
                                 period=period, day=day, admin=admin)
