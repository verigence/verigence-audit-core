"""Task Manager: tasks that are not tied to a Journey, recurring tasks and the
Daily Gate Pass task (product owner, 2026-10-26; docs/TASK_MANAGER_DESIGN_v1.0.md).

Task Manager tasks live in ``p2_tasks`` beside Journey tasks, with
``scope_kind`` OUTLET or PROJECT and no Journey. Their lifecycle is kept here,
apart from the Journey task lifecycle (uc03_p2_tasks), which is untouched:

- one-off tasks: the opener sets the expected time to close; the assignee
  completes with a comment; the opener reviews within 2 days. A task assigned
  to a PM has no reviewer.
- recurring tasks (``p2_task_schedules``): created by an Executive or
  Superadmin; a PM reviews every occurrence (none when the assignee is a PM,
  the Executive when the Project has no PM). No daily occurrence on a Sunday;
  a weekly or monthly one that falls on a Sunday moves to the Monday. An
  occurrence still open when the next is generated is marked Missed.
- the Daily Gate Pass task: one per outlet with an active PC and working day,
  closed by the system when that outlet's gate pass of the day is finished,
  incomplete when it is not finished by 23:59 IST the next day.

Everything is per Project: every query runs under the tenant's RLS context.
"""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import Connection, text

from audit_core.uc03_p2_object_keys import IST
from audit_core.uc03_p2_runtime import claim_scheduled_run

logger = structlog.get_logger(__name__)

SUPER_ADMIN = "SUPER_ADMIN"
EXECUTIVE = "EXECUTIVE"
ALL_ROLES = ("PC", "TL", "PM", "CRM", "REVIEWER", EXECUTIVE)

# Who may raise a Task Manager task to whom (product owner, 2026-10-26).
ASSIGN_MATRIX: dict[str, frozenset[str]] = {
    SUPER_ADMIN: frozenset(ALL_ROLES),
    EXECUTIVE: frozenset(ALL_ROLES),
    "PM": frozenset({"TL", "PC", "CRM"}),
    "TL": frozenset({"PC", "CRM"}),
    "CRM": frozenset({"TL", "PM"}),
    "REVIEWER": frozenset({"PC"}),
    "PC": frozenset(),
}
SCHEDULE_OWNERS = frozenset({EXECUTIVE, SUPER_ADMIN})
# Besides the raiser: who may reassign or cancel any task of the Project.
REASSIGNERS = frozenset({"PM", EXECUTIVE, SUPER_ADMIN})
CANCELLERS = frozenset({EXECUTIVE, SUPER_ADMIN})
# Who sees every Task Manager task of the Project (view=all).
OVERSEERS = frozenset({"PM", EXECUTIVE, SUPER_ADMIN})

REVIEW_SLA = timedelta(days=2)
MAX_EXPECTED_MINUTES = 60 * 24 * 60

ONE_OFF_TASK = "TM_TASK"
RECURRING_TASK = "TM_RECURRING"
GATE_PASS_TASK = "GATE_PASS_DAILY"
GATE_PASS_SYSTEM_KIND = "GATE_PASS"
GATE_PASS_MISMATCH_TASK = "GATE_PASS_DELIVERY_NOT_NOTIFIED"
CATEGORY = "TASK_MANAGER"

TERMINAL = frozenset({"VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER"})
ASSIGNEE_OPEN = frozenset({"READY", "IN_PROGRESS", "RETURNED"})
AWAITING_REVIEW = "AWAITING_REQUESTER_REVIEW"

ACTIONS = ("START", "COMPLETE_ACTION", "ACCEPT", "REJECT", "ADD_COMMENT", "REASSIGN", "CANCEL")
_COMMENT_REQUIRED = frozenset({"COMPLETE_ACTION", "REJECT", "ADD_COMMENT", "CANCEL"})


class TaskManagerError(ValueError):
    """A request the rules do not allow (422)."""


class TaskManagerForbidden(ValueError):
    """The caller may not do this (403)."""


class TaskManagerConflict(ValueError):
    """The task's state does not allow this (409)."""


class TaskManagerNotFound(LookupError):
    """Not found in the caller's view of the Project (404)."""


@dataclass(frozen=True)
class Caller:
    actor_id: str
    role: str | None
    display_name: str | None = None

    @property
    def is_super_admin(self) -> bool:
        return self.role == SUPER_ADMIN


def normalise_role(value: Any) -> str | None:
    role = str(value or "").strip().upper()
    if not role:
        return None
    return EXECUTIVE if role in {"EXEC", EXECUTIVE} else role


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _clean(value: str | None, *, limit: int) -> str:
    return (value or "").strip()[:limit]


# --------------------------------------------------------------------------- people

_ACTIVE = """
    ba.assignment_status = 'ACTIVE' AND ba.effective_from <= now()
    AND (ba.effective_to IS NULL OR ba.effective_to >= now())
"""


def caller_role(connection: Connection, *, tenant_id: str, actor_id: str) -> str | None:
    """The caller's operating role in the Project (one per person and Project)."""
    roles = connection.execute(
        text(f"""
            SELECT DISTINCT upper(ba.business_role_code)
            FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = :t AND ba.security_actor_id = :a AND {_ACTIVE}
        """),
        {"t": tenant_id, "a": actor_id},
    ).scalars().all()
    normalised = sorted({normalise_role(r) for r in roles if r})
    if not normalised:
        return None
    for preferred in (EXECUTIVE, "PM", "TL", "CRM", "REVIEWER", "PC"):
        if preferred in normalised:
            return preferred
    return normalised[0]


def known_names(connection: Connection, *, tenant_id: str) -> dict[str, str]:
    """The best known display name of each person of the Project: the name the
    app last reported for them, else the name on what they did (journeys, gate
    passes, tasks). Audit Core has no people directory of its own."""
    rows = connection.execute(
        text("""
            SELECT actor_id, display_name, rank FROM (
              SELECT DISTINCT ON (actor_id) actor_id, display_name, 1 AS rank
              FROM auditcore.p2_user_activity_daily
              WHERE tenant_id = :t AND display_name IS NOT NULL AND display_name <> ''
              ORDER BY actor_id, activity_date DESC, updated_at_utc DESC
            ) a
            UNION ALL
            SELECT actor_id, display_name, rank FROM (
              SELECT DISTINCT ON (created_by_actor_id) created_by_actor_id AS actor_id,
                     created_by_display_name AS display_name, 2 AS rank
              FROM auditcore.journeys
              WHERE tenant_id = :t AND created_by_actor_id IS NOT NULL AND created_by_display_name IS NOT NULL
              ORDER BY created_by_actor_id, created_at_utc DESC
            ) j
            UNION ALL
            SELECT actor_id, display_name, rank FROM (
              SELECT DISTINCT ON (pc_actor_id) pc_actor_id AS actor_id, pc_name AS display_name, 3 AS rank
              FROM auditcore.p2_gate_passes
              WHERE tenant_id = :t AND pc_name IS NOT NULL AND pc_name <> pc_actor_id
              ORDER BY pc_actor_id, created_at_utc DESC
            ) g
            UNION ALL
            SELECT actor_id, display_name, rank FROM (
              SELECT DISTINCT ON (raised_by_actor_id) raised_by_actor_id AS actor_id,
                     raised_by_display_name AS display_name, 4 AS rank
              FROM auditcore.p2_tasks
              WHERE tenant_id = :t AND raised_by_actor_id IS NOT NULL AND raised_by_display_name IS NOT NULL
              ORDER BY raised_by_actor_id, created_at_utc DESC
            ) r
        """),
        {"t": tenant_id},
    ).mappings().all()
    names: dict[str, tuple[int, str]] = {}
    for row in rows:
        actor, name, rank = str(row["actor_id"]), str(row["display_name"]).strip(), int(row["rank"])
        if name and (actor not in names or rank < names[actor][0]):
            names[actor] = (rank, name)
    return {actor: name for actor, (_, name) in names.items()}


def project_people(connection: Connection, *, tenant_id: str) -> list[dict[str, Any]]:
    """Everyone with an active business assignment in the Project: role, the
    outlets they are mapped to (none = Project-wide) and their name."""
    rows = connection.execute(
        text(f"""
            SELECT ba.security_actor_id AS actor_id, upper(ba.business_role_code) AS role,
                   ba.dealer_id, ba.outlet_id, o.outlet_name
            FROM auditcore.business_assignments ba
            LEFT JOIN auditcore.dealer_outlets o
              ON o.tenant_id = ba.tenant_id AND o.outlet_id = ba.outlet_id
            WHERE ba.tenant_id = :t AND {_ACTIVE}
            ORDER BY ba.security_actor_id
        """),
        {"t": tenant_id},
    ).mappings().all()
    names = known_names(connection, tenant_id=tenant_id)
    people: dict[str, dict[str, Any]] = {}
    for row in rows:
        actor = str(row["actor_id"])
        person = people.setdefault(actor, {
            "actorId": actor, "roleCode": normalise_role(row["role"]), "displayName": names.get(actor),
            "outlets": [], "projectWide": False,
        })
        if row["outlet_id"] is None and row["dealer_id"] is None:
            person["projectWide"] = True
        elif row["outlet_id"] is not None:
            person["outlets"].append({"outletId": str(row["outlet_id"]), "outletName": row["outlet_name"]})
    return sorted(people.values(), key=lambda p: (p["roleCode"] or "", (p["displayName"] or p["actorId"]).lower()))


def _person(people: list[dict[str, Any]], actor_id: str) -> dict[str, Any] | None:
    return next((p for p in people if p["actorId"] == actor_id), None)


def assignable_roles(caller: Caller) -> frozenset[str]:
    return ASSIGN_MATRIX.get(caller.role or "", frozenset())


def _caller_outlets(connection: Connection, *, tenant_id: str, actor_id: str) -> tuple[bool, list[str]]:
    """(Project-wide, outlet ids) of the caller's own assignments."""
    rows = connection.execute(
        text(f"""
            SELECT ba.dealer_id, ba.outlet_id FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = :t AND ba.security_actor_id = :a AND {_ACTIVE}
        """),
        {"t": tenant_id, "a": actor_id},
    ).mappings().all()
    wide = any(r["dealer_id"] is None and r["outlet_id"] is None for r in rows)
    dealers = [str(r["dealer_id"]) for r in rows if r["dealer_id"] is not None and r["outlet_id"] is None]
    outlets = {str(r["outlet_id"]) for r in rows if r["outlet_id"] is not None}
    if dealers:
        outlets |= set(connection.execute(
            text("SELECT outlet_id::text FROM auditcore.dealer_outlets WHERE tenant_id = :t "
                 "AND dealer_id = ANY(CAST(:d AS uuid[]))"),
            {"t": tenant_id, "d": dealers},
        ).scalars().all())
    return wide, sorted(outlets)


# --------------------------------------------------------------------------- rows

_TASK_COLUMNS = """
    t.task_id, t.scope_kind, t.journey_id, t.dealer_id, t.outlet_id, o.outlet_name,
    t.task_type, t.category, t.origin_kind, t.source_type, t.title, t.description, t.reference,
    t.severity, t.priority, t.assigned_role_code, t.assigned_actor_id, t.assigned_display_name,
    t.raised_by_actor_id, t.raised_by_role_code, t.raised_by_display_name,
    t.reviewer_role_code, t.reviewer_actor_id, t.review_due_at_utc,
    t.completion_protocol, t.task_status, t.round_number, t.expected_close_minutes,
    t.due_at_utc, t.created_at_utc, t.updated_at_utc, t.verified_at_utc,
    t.action_completed_at_utc, t.completed_by_actor_id, t.schedule_id, t.occurrence_date,
    t.missed_at_utc, t.completion_result,
    (SELECT COUNT(*) FROM auditcore.p2_task_events ev
      WHERE ev.tenant_id = t.tenant_id AND ev.task_id = t.task_id AND ev.comment IS NOT NULL) AS comment_count
"""
_FROM = """
    FROM auditcore.p2_tasks t
    LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = t.tenant_id AND o.outlet_id = t.outlet_id
"""


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _is_assignee(task: dict[str, Any], caller: Caller, outlets: tuple[bool, list[str]] | None) -> bool:
    if task["assigned_actor_id"]:
        return str(task["assigned_actor_id"]) == caller.actor_id
    if normalise_role(task["assigned_role_code"]) != caller.role:
        return False
    if task["outlet_id"] is None or outlets is None:
        return True
    wide, ids = outlets
    return wide or str(task["outlet_id"]) in ids


def _is_reviewer(task: dict[str, Any], caller: Caller) -> bool:
    if task["completion_protocol"] != "REQUESTER_CONFIRMED":
        return False
    if task["reviewer_actor_id"]:
        return str(task["reviewer_actor_id"]) == caller.actor_id
    return bool(task["reviewer_role_code"]) and normalise_role(task["reviewer_role_code"]) == caller.role


def _owns_schedule(connection: Connection, *, tenant_id: str, task: dict[str, Any], caller: Caller) -> bool:
    if task.get("schedule_id") is None:
        return False
    owner = connection.execute(
        text("SELECT created_by_actor_id FROM auditcore.p2_task_schedules WHERE tenant_id = :t AND schedule_id = :s"),
        {"t": tenant_id, "s": str(task["schedule_id"])},
    ).scalar_one_or_none()
    return owner is not None and str(owner) == caller.actor_id


def my_actions(task: dict[str, Any], caller: Caller, *, outlets: tuple[bool, list[str]] | None = None,
               owns_schedule: bool = False) -> list[str]:
    status = str(task["task_status"])
    if status in TERMINAL:
        return []
    actions: list[str] = []
    system = task["completion_protocol"] == "SYSTEM_VERIFIED"
    if _is_assignee(task, caller, outlets) and not system:
        if status == "READY":
            actions.append("START")
        if status in ASSIGNEE_OPEN:
            actions.append("COMPLETE_ACTION")
    if status == AWAITING_REVIEW and _is_reviewer(task, caller) and task["completed_by_actor_id"] != caller.actor_id:
        actions += ["ACCEPT", "REJECT"]
    raiser = task["raised_by_actor_id"] is not None and str(task["raised_by_actor_id"]) == caller.actor_id
    if not system and task["assigned_actor_id"] and status in ASSIGNEE_OPEN and (raiser or caller.role in REASSIGNERS):
        actions.append("REASSIGN")
    if not system and (raiser or owns_schedule or caller.role in CANCELLERS):
        actions.append("CANCEL")
    actions.append("ADD_COMMENT")
    return actions


def public_task(row: dict[str, Any], caller: Caller, names: dict[str, str], *,
                outlets: tuple[bool, list[str]] | None = None, owns_schedule: bool = False,
                now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    status = str(row["task_status"])
    due = row["due_at_utc"]
    review_due = row["review_due_at_utc"]
    assigned = row["assigned_actor_id"]
    return {
        "taskId": str(row["task_id"]),
        "scope": row["scope_kind"],
        "outletId": str(row["outlet_id"]) if row["outlet_id"] else None,
        "outletName": row["outlet_name"] or (row["reference"] or {}).get("outletName"),
        "taskType": row["task_type"],
        "category": row["category"],
        "kind": ("GATE_PASS" if row["task_type"] == GATE_PASS_TASK
                 else "RECURRING" if row["schedule_id"] else
                 "SYSTEM" if row["origin_kind"] == "SYSTEM" else "ONE_OFF"),
        "title": row["title"],
        "description": row["description"],
        "reference": row["reference"] or {},
        "priority": row["priority"],
        "severity": row["severity"],
        "status": status,
        "assignedRole": normalise_role(row["assigned_role_code"]),
        "assignedActorId": assigned,
        "assignedName": (row["assigned_display_name"] or names.get(str(assigned))) if assigned else None,
        "raisedByActorId": row["raised_by_actor_id"],
        "raisedByRole": normalise_role(row["raised_by_role_code"]),
        "raisedByName": row["raised_by_display_name"] or names.get(str(row["raised_by_actor_id"] or "")),
        "reviewerRole": normalise_role(row["reviewer_role_code"]),
        "reviewerActorId": row["reviewer_actor_id"],
        "reviewerName": names.get(str(row["reviewer_actor_id"] or "")) if row["reviewer_actor_id"] else None,
        "completionProtocol": row["completion_protocol"],
        "expectedCloseMinutes": row["expected_close_minutes"],
        "dueAtUtc": _iso(due),
        "reviewDueAtUtc": _iso(review_due),
        "createdAtUtc": _iso(row["created_at_utc"]),
        "updatedAtUtc": _iso(row["updated_at_utc"]),
        "completedAtUtc": _iso(row["action_completed_at_utc"]),
        "completedByActorId": row["completed_by_actor_id"],
        "completedByName": names.get(str(row["completed_by_actor_id"] or "")) if row["completed_by_actor_id"] else None,
        "closedAtUtc": _iso(row["verified_at_utc"]),
        "scheduleId": str(row["schedule_id"]) if row["schedule_id"] else None,
        "occurrenceDate": _iso(row["occurrence_date"]),
        "sendBacks": max(int(row["round_number"] or 1) - 1, 0),
        "overdue": status in ASSIGNEE_OPEN and due is not None and due < now,
        "reviewOverdue": status == AWAITING_REVIEW and review_due is not None and review_due < now,
        "missed": row["missed_at_utc"] is not None and status not in {"VERIFIED_COMPLETE", "CANCELLED"},
        "incomplete": status == "FAILED" and (row["completion_result"] or {}).get("outcome") == "INCOMPLETE",
        "closedLate": (row["action_completed_at_utc"] is not None and due is not None
                       and row["action_completed_at_utc"] > due),
        "commentCount": int(row.get("comment_count") or 0),
        "myActions": my_actions(row, caller, outlets=outlets, owns_schedule=owns_schedule),
    }


# --------------------------------------------------------------------------- write helpers


def _event(connection: Connection, *, tenant_id: str, task_id: UUID | str, event_type: str, caller: Caller | None,
           comment: str | None = None, details: dict[str, Any] | None = None) -> None:
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_task_events (tenant_id, task_id, journey_id, event_type,
                                                  actor_id, actor_role_code, comment, details)
            VALUES (:t, :id, NULL, :type, :actor, :role, :comment, CAST(:details AS jsonb))
        """),
        {"t": tenant_id, "id": str(task_id), "type": event_type,
         "actor": caller.actor_id if caller else "SYSTEM", "role": caller.role if caller else "SYSTEM",
         "comment": comment, "details": _json(details or {})},
    )


def _insert_task(connection: Connection, *, tenant_id: str, values: dict[str, Any]) -> UUID | None:
    """Insert one Task Manager task; None when its dedupe key already exists."""
    task_id = values.get("task_id") or uuid4()
    row = connection.execute(
        text("""
            INSERT INTO auditcore.p2_tasks (
              tenant_id, task_id, root_task_id, journey_id, scope_kind, dealer_id, outlet_id,
              task_type, category, origin_kind, source_type, source_code, dedupe_key,
              title, description, reference, severity, priority,
              assigned_role_code, assigned_actor_id, assigned_display_name,
              raised_by_actor_id, raised_by_role_code, raised_by_display_name,
              reviewer_role_code, reviewer_actor_id, allowed_actions, completion_protocol,
              expected_close_minutes, due_at_utc, schedule_id, occurrence_date
            ) VALUES (
              :t, :id, :id, NULL, :scope, :dealer, :outlet,
              :type, :category, :origin, :source_type, :source_code, :dedupe,
              :title, :description, CAST(:reference AS jsonb), 'MEDIUM', :priority,
              :role, :actor, :actor_name,
              :raised_by, :raised_role, :raised_name,
              :reviewer_role, :reviewer_actor, CAST(:allowed AS jsonb), :protocol,
              :minutes, :due, :schedule, :occurrence
            )
            ON CONFLICT (tenant_id, dedupe_key) DO NOTHING
            RETURNING task_id
        """),
        {
            "t": tenant_id, "id": str(task_id), "scope": values["scope"],
            "dealer": values.get("dealer_id"), "outlet": values.get("outlet_id"),
            "type": values["task_type"], "category": values.get("category") or CATEGORY,
            "origin": values.get("origin", "HUMAN"), "source_type": values.get("source_type", "TASK_MANAGER"),
            "source_code": values.get("source_code"), "dedupe": values["dedupe_key"],
            "title": values["title"], "description": values["description"],
            "reference": _json(values.get("reference") or {}), "priority": values.get("priority") or "NORMAL",
            "role": values["assigned_role"], "actor": values.get("assigned_actor"),
            "actor_name": values.get("assigned_name"),
            "raised_by": values.get("raised_by"), "raised_role": values.get("raised_role"),
            "raised_name": values.get("raised_name"),
            "reviewer_role": values.get("reviewer_role"), "reviewer_actor": values.get("reviewer_actor"),
            "allowed": _json(["START", "COMPLETE_ACTION", "ADD_COMMENT"]), "protocol": values["protocol"],
            "minutes": values.get("expected_close_minutes"), "due": values.get("due"),
            "schedule": values.get("schedule_id"), "occurrence": values.get("occurrence_date"),
        },
    ).scalar_one_or_none()
    return UUID(str(row)) if row is not None else None


def _reviewer_for_one_off(assignee_role: str, raiser: Caller) -> tuple[str, str | None, str | None]:
    """(protocol, reviewer role, reviewer actor): the opener reviews, except
    for a task assigned to a PM, which has no reviewer."""
    if assignee_role == "PM":
        return "SELF_CLOSING", None, None
    return "REQUESTER_CONFIRMED", raiser.role, raiser.actor_id


def _project_has(connection: Connection, *, tenant_id: str, role: str) -> bool:
    return connection.execute(
        text(f"""
            SELECT 1 FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = :t AND {_ACTIVE} AND upper(ba.business_role_code) = ANY(:roles) LIMIT 1
        """),
        {"t": tenant_id, "roles": [role, "EXEC"] if role == EXECUTIVE else [role]},
    ).scalar_one_or_none() is not None


def _reviewer_for_recurring(connection: Connection, *, tenant_id: str, assignee_role: str) -> tuple[str, str | None]:
    """(protocol, reviewer role): a PM reviews every occurrence; none when the
    assignee is a PM; the Executive when the Project has no PM."""
    if assignee_role == "PM":
        return "SELF_CLOSING", None
    if _project_has(connection, tenant_id=tenant_id, role="PM"):
        return "REQUESTER_CONFIRMED", "PM"
    return "REQUESTER_CONFIRMED", EXECUTIVE


def _outlet(connection: Connection, *, tenant_id: str, outlet_id: UUID | str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT dealer_id, outlet_id, outlet_name FROM auditcore.dealer_outlets "
             "WHERE tenant_id = :t AND outlet_id = :o"),
        {"t": tenant_id, "o": str(outlet_id)},
    ).mappings().one_or_none()
    if row is None:
        raise TaskManagerError("That outlet is not in this Project.")
    return dict(row)


# --------------------------------------------------------------------------- one-off tasks


def raise_tasks(
    connection: Connection, *, tenant_id: str, caller: Caller, title: str, description: str,
    assignee_actor_ids: list[str], expected_close_minutes: int, priority: str = "NORMAL",
    outlet_id: UUID | None = None, now: datetime | None = None,
) -> list[str]:
    """Raise one task per assignee. The matrix decides who may assign to whom."""
    allowed = assignable_roles(caller)
    if not allowed:
        raise TaskManagerForbidden("Your role cannot assign tasks.")
    title, description = _clean(title, limit=300), _clean(description, limit=4000)
    if not title or not description:
        raise TaskManagerError("A title and a description are required.")
    if not 1 <= int(expected_close_minutes or 0) <= MAX_EXPECTED_MINUTES:
        raise TaskManagerError("Set the expected time to close the task.")
    if priority not in {"URGENT", "HIGH", "NORMAL", "LOW"}:
        raise TaskManagerError("Unknown priority.")
    wanted = list(dict.fromkeys(a.strip() for a in assignee_actor_ids if a and a.strip()))
    if not wanted:
        raise TaskManagerError("Choose at least one person.")
    if caller.actor_id in wanted:
        raise TaskManagerError("You cannot assign a task to yourself.")
    people = project_people(connection, tenant_id=tenant_id)
    outlet = _outlet(connection, tenant_id=tenant_id, outlet_id=outlet_id) if outlet_id else None
    now = now or datetime.now(UTC)
    created: list[str] = []
    for actor in wanted:
        person = _person(people, actor)
        if person is None:
            raise TaskManagerError("One of the people chosen is not active in this Project.")
        role = person["roleCode"]
        if role not in allowed:
            raise TaskManagerForbidden(f"You cannot assign a task to a {role}.")
        protocol, reviewer_role, reviewer_actor = _reviewer_for_one_off(role, caller)
        task_id = _insert_task(connection, tenant_id=tenant_id, values={
            "scope": "OUTLET" if outlet else "PROJECT",
            "dealer_id": str(outlet["dealer_id"]) if outlet else None,
            "outlet_id": str(outlet["outlet_id"]) if outlet else None,
            "task_type": ONE_OFF_TASK, "dedupe_key": f"tm:{uuid4()}",
            "title": title, "description": description, "priority": priority,
            "reference": {"outletName": outlet["outlet_name"]} if outlet else {},
            "assigned_role": role, "assigned_actor": actor, "assigned_name": person["displayName"],
            "raised_by": caller.actor_id, "raised_role": caller.role, "raised_name": caller.display_name,
            "reviewer_role": reviewer_role, "reviewer_actor": reviewer_actor, "protocol": protocol,
            "expected_close_minutes": int(expected_close_minutes),
            "due": now + timedelta(minutes=int(expected_close_minutes)),
        })
        assert task_id is not None
        _event(connection, tenant_id=tenant_id, task_id=task_id, event_type="RAISED", caller=caller,
               details={"assignedActorId": actor, "assignedRole": role, "expectedCloseMinutes": expected_close_minutes})
        created.append(str(task_id))
    return created


# --------------------------------------------------------------------------- reading


def _visibility(caller: Caller, view: str) -> tuple[str, dict[str, Any]]:
    """SQL predicate for which Task Manager tasks the caller sees in a view."""
    params: dict[str, Any] = {"me": caller.actor_id, "role": caller.role or "", "all_outlets": True, "outlets": []}
    mine = """(
        t.assigned_actor_id = :me
        OR (t.assigned_actor_id IS NULL AND upper(t.assigned_role_code) = :role
            AND (t.outlet_id IS NULL OR CAST(:all_outlets AS boolean) OR t.outlet_id::text = ANY(:outlets)))
    )"""
    review = """(
        t.completion_protocol = 'REQUESTER_CONFIRMED' AND t.task_status = 'AWAITING_REQUESTER_REVIEW'
        AND (t.reviewer_actor_id = :me
             OR (t.reviewer_actor_id IS NULL AND upper(t.reviewer_role_code) = :role))
    )"""
    raised = "(t.raised_by_actor_id = :me)"
    if view == "mine":
        return mine, params
    if view == "review":
        return review, params
    if view == "raised":
        return raised, params
    if view == "all":
        if caller.role in OVERSEERS:
            return "TRUE", params
        return f"({mine} OR {review} OR {raised})", params
    raise TaskManagerError("Unknown view.")


def list_tasks(
    connection: Connection, *, tenant_id: str, caller: Caller, view: str = "mine", status: str = "open",
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    outlets = None if caller.is_super_admin else _caller_outlets(connection, tenant_id=tenant_id,
                                                                     actor_id=caller.actor_id)
    status_sql = {
        "open": "t.task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')",
        "done": ("t.task_status IN ('VERIFIED_COMPLETE','CANCELLED','FAILED') "
                 "AND t.updated_at_utc > now() - interval '14 days'"),
        "all": "TRUE",
    }.get(status)
    if status_sql is None:
        raise TaskManagerError("Unknown status filter.")
    names = known_names(connection, tenant_id=tenant_id)
    counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for each in ("mine", "review", "raised", "all"):
        predicate, params = _visibility(caller, each)
        if outlets is not None:
            params["all_outlets"], params["outlets"] = outlets
        if each == view:
            rows = connection.execute(
                text(f"""
                    SELECT {_TASK_COLUMNS} {_FROM}
                    WHERE t.tenant_id = :t AND t.scope_kind <> 'JOURNEY' AND {predicate} AND {status_sql}
                    ORDER BY (t.task_status IN ('VERIFIED_COMPLETE','CANCELLED','FAILED')),
                             t.due_at_utc NULLS LAST, t.created_at_utc DESC
                    LIMIT 500
                """),
                {"t": tenant_id, **params},
            ).mappings().all()
            items = [public_task(dict(r), caller, names, outlets=outlets, now=now) for r in rows]
        counts[each.upper()] = int(connection.execute(
            text(f"""
                SELECT COUNT(*) FROM auditcore.p2_tasks t
                WHERE t.tenant_id = :t AND t.scope_kind <> 'JOURNEY' AND {predicate}
                  AND t.task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
            """),
            {"t": tenant_id, **params},
        ).scalar_one())
    # What needs the caller now: their open work plus what waits for their review.
    counts["ACTION"] = int(connection.execute(
        text(f"""
            SELECT COUNT(*) FROM auditcore.p2_tasks t
            WHERE t.tenant_id = :t AND t.scope_kind <> 'JOURNEY'
              AND (({_visibility(caller, 'mine')[0]} AND t.task_status IN ('READY','IN_PROGRESS','RETURNED')
                    AND t.completion_protocol <> 'SYSTEM_VERIFIED')
                   OR (t.task_type = :gate AND {_visibility(caller, 'mine')[0]}
                       AND t.task_status = 'READY')
                   OR {_visibility(caller, 'review')[0]})
        """),
        {"t": tenant_id, "gate": GATE_PASS_TASK,
         **_visibility(caller, "mine")[1],
            **({"all_outlets": outlets[0], "outlets": outlets[1]} if outlets is not None else {})},
    ).scalar_one())
    return {"items": items, "counts": counts, "role": caller.role}


def _load(connection: Connection, *, tenant_id: str, task_id: UUID, lock: bool = False) -> dict[str, Any]:
    row = connection.execute(
        text(f"SELECT {_TASK_COLUMNS} {_FROM} WHERE t.tenant_id = :t AND t.task_id = :id "
             "AND t.scope_kind <> 'JOURNEY'" + (" FOR UPDATE OF t" if lock else "")),
        {"t": tenant_id, "id": str(task_id)},
    ).mappings().one_or_none()
    if row is None:
        raise TaskManagerNotFound("Task not found.")
    return dict(row)


def _can_see(connection: Connection, *, tenant_id: str, task: dict[str, Any], caller: Caller,
             outlets: tuple[bool, list[str]] | None) -> bool:
    if caller.is_super_admin or caller.role in OVERSEERS:
        return True
    return (_is_assignee(task, caller, outlets) or _is_reviewer(task, caller)
            or str(task["raised_by_actor_id"] or "") == caller.actor_id
            or _owns_schedule(connection, tenant_id=tenant_id, task=task, caller=caller))


def get_task(connection: Connection, *, tenant_id: str, caller: Caller, task_id: UUID) -> dict[str, Any]:
    task = _load(connection, tenant_id=tenant_id, task_id=task_id)
    outlets = None if caller.is_super_admin else _caller_outlets(connection, tenant_id=tenant_id,
                                                                     actor_id=caller.actor_id)
    if not _can_see(connection, tenant_id=tenant_id, task=task, caller=caller, outlets=outlets):
        raise TaskManagerNotFound("Task not found.")
    names = known_names(connection, tenant_id=tenant_id)
    owns = _owns_schedule(connection, tenant_id=tenant_id, task=task, caller=caller)
    item = public_task(task, caller, names, outlets=outlets, owns_schedule=owns)
    events = connection.execute(
        text("""
            SELECT task_event_id, event_type, actor_id, actor_role_code, comment, details, created_at_utc
            FROM auditcore.p2_task_events WHERE tenant_id = :t AND task_id = :id ORDER BY task_event_id
        """),
        {"t": tenant_id, "id": str(task_id)},
    ).mappings().all()
    item["events"] = [{
        "eventType": e["event_type"], "actorId": e["actor_id"], "actorRole": normalise_role(e["actor_role_code"]),
        "actorName": names.get(str(e["actor_id"] or "")) or (None if e["actor_id"] != "SYSTEM" else "System"),
        "comment": e["comment"], "details": e["details"] or {}, "atUtc": _iso(e["created_at_utc"]),
    } for e in events]
    return item


# --------------------------------------------------------------------------- actions


def act(
    connection: Connection, *, tenant_id: str, caller: Caller, task_id: UUID, action: str,
    comment: str | None = None, details: dict[str, Any] | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    action = str(action or "").strip().upper()
    if action not in ACTIONS:
        raise TaskManagerError("Unknown action.")
    comment = _clean(comment, limit=4000) or None
    details = details or {}
    if action in _COMMENT_REQUIRED and not comment:
        raise TaskManagerError("Write a comment for this action.")
    now = now or datetime.now(UTC)
    task = _load(connection, tenant_id=tenant_id, task_id=task_id, lock=True)
    outlets = None if caller.is_super_admin else _caller_outlets(connection, tenant_id=tenant_id,
                                                                     actor_id=caller.actor_id)
    if not _can_see(connection, tenant_id=tenant_id, task=task, caller=caller, outlets=outlets):
        raise TaskManagerNotFound("Task not found.")
    owns = _owns_schedule(connection, tenant_id=tenant_id, task=task, caller=caller)
    allowed = my_actions(task, caller, outlets=outlets, owns_schedule=owns)
    if action not in allowed:
        if str(task["task_status"]) in TERMINAL:
            raise TaskManagerConflict(f"This task is already {str(task['task_status']).replace('_', ' ').lower()}.")
        raise TaskManagerForbidden("You cannot do that on this task now.")
    params = {"t": tenant_id, "id": str(task_id), "now": now, "me": caller.actor_id}

    if action == "START":
        connection.execute(text("UPDATE auditcore.p2_tasks SET task_status = 'IN_PROGRESS', updated_at_utc = :now "
                                "WHERE tenant_id = :t AND task_id = :id"), params)
        _event(connection, tenant_id=tenant_id, task_id=task_id, event_type="STARTED", caller=caller, comment=comment)
    elif action == "COMPLETE_ACTION":
        if task["completion_protocol"] == "REQUESTER_CONFIRMED":
            connection.execute(text("""
                UPDATE auditcore.p2_tasks
                SET task_status = 'AWAITING_REQUESTER_REVIEW', action_completed_at_utc = :now,
                    completed_by_actor_id = :me, review_due_at_utc = :review_due, updated_at_utc = :now
                WHERE tenant_id = :t AND task_id = :id
            """), {**params, "review_due": now + REVIEW_SLA})
        else:
            connection.execute(text("""
                UPDATE auditcore.p2_tasks
                SET task_status = 'VERIFIED_COMPLETE', action_completed_at_utc = :now, completed_by_actor_id = :me,
                    verified_at_utc = :now, updated_at_utc = :now
                WHERE tenant_id = :t AND task_id = :id
            """), params)
        _event(connection, tenant_id=tenant_id, task_id=task_id, event_type="COMPLETE_ACTION", caller=caller,
               comment=comment, details={"late": bool(task["due_at_utc"] and now > task["due_at_utc"])})
    elif action == "ACCEPT":
        connection.execute(text("""
            UPDATE auditcore.p2_tasks SET task_status = 'VERIFIED_COMPLETE', verified_at_utc = :now,
                   updated_at_utc = :now,
                   completion_result = completion_result || jsonb_build_object('acceptedBy', CAST(:me AS text))
            WHERE tenant_id = :t AND task_id = :id
        """), params)
        _event(connection, tenant_id=tenant_id, task_id=task_id, event_type="ACCEPT", caller=caller, comment=comment,
               details={"reviewLate": bool(task["review_due_at_utc"] and now > task["review_due_at_utc"])})
    elif action == "REJECT":
        connection.execute(text("""
            UPDATE auditcore.p2_tasks
            SET task_status = 'RETURNED', round_number = round_number + 1, review_due_at_utc = NULL,
                action_completed_at_utc = NULL, completed_by_actor_id = NULL, updated_at_utc = :now
            WHERE tenant_id = :t AND task_id = :id
        """), params)
        _event(connection, tenant_id=tenant_id, task_id=task_id, event_type="REJECT", caller=caller, comment=comment,
               details={"sendBack": int(task["round_number"] or 1)})
    elif action == "ADD_COMMENT":
        connection.execute(text("UPDATE auditcore.p2_tasks SET updated_at_utc = :now "
                                "WHERE tenant_id = :t AND task_id = :id"), params)
        _event(connection, tenant_id=tenant_id, task_id=task_id, event_type="ADD_COMMENT", caller=caller,
               comment=comment)
    elif action == "REASSIGN":
        _reassign(connection, tenant_id=tenant_id, caller=caller, task=task,
                  target=str(details.get("assigneeActorId") or "").strip(), comment=comment, now=now)
    elif action == "CANCEL":
        connection.execute(text("UPDATE auditcore.p2_tasks SET task_status = 'CANCELLED', updated_at_utc = :now "
                                "WHERE tenant_id = :t AND task_id = :id"), params)
        _event(connection, tenant_id=tenant_id, task_id=task_id, event_type="CANCELLED", caller=caller,
               comment=comment)
    return get_task(connection, tenant_id=tenant_id, caller=caller, task_id=task_id)


def _reassign(connection: Connection, *, tenant_id: str, caller: Caller, task: dict[str, Any], target: str,
              comment: str | None, now: datetime) -> None:
    if not target:
        raise TaskManagerError("Choose who the task goes to.")
    if target == str(task["assigned_actor_id"] or ""):
        raise TaskManagerError("The task is already with that person.")
    people = project_people(connection, tenant_id=tenant_id)
    person = _person(people, target)
    if person is None:
        raise TaskManagerError("That person is not active in this Project.")
    role = person["roleCode"]
    if role not in assignable_roles(caller):
        raise TaskManagerForbidden(f"You cannot assign a task to a {role}.")
    if task["schedule_id"]:
        protocol, reviewer_role = _reviewer_for_recurring(connection, tenant_id=tenant_id, assignee_role=role)
        reviewer_actor = None
    else:
        raiser = Caller(actor_id=str(task["raised_by_actor_id"] or caller.actor_id),
                        role=normalise_role(task["raised_by_role_code"]) or caller.role)
        protocol, reviewer_role, reviewer_actor = _reviewer_for_one_off(role, raiser)
    connection.execute(
        text("""
            UPDATE auditcore.p2_tasks
            SET assigned_actor_id = :actor, assigned_role_code = :role, assigned_display_name = :name,
                completion_protocol = :protocol, reviewer_role_code = :reviewer_role,
                reviewer_actor_id = :reviewer_actor, updated_at_utc = :now
            WHERE tenant_id = :t AND task_id = :id
        """),
        {"t": tenant_id, "id": str(task["task_id"]), "actor": target, "role": role, "name": person["displayName"],
         "protocol": protocol, "reviewer_role": reviewer_role, "reviewer_actor": reviewer_actor, "now": now},
    )
    _event(connection, tenant_id=tenant_id, task_id=task["task_id"], event_type="REASSIGNED", caller=caller,
           comment=comment, details={"from": task["assigned_actor_id"], "to": target, "toRole": role})


# --------------------------------------------------------------------------- schedules

_SCHEDULE_FIELDS = """
    schedule_id, system_kind, title, description, category, priority, frequency, weekdays, month_day,
    run_time_ist, expected_close_minutes, assignee_mode, assigned_role_code, assigned_actor_id,
    assigned_display_name, outlet_id, starts_on, ends_on, skip_dates, schedule_status,
    created_by_actor_id, created_by_role_code, created_by_display_name, created_at_utc, updated_at_utc
"""


def _public_schedule(row: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    return {
        "scheduleId": str(row["schedule_id"]), "systemKind": row["system_kind"], "title": row["title"],
        "description": row["description"], "category": row["category"], "priority": row["priority"],
        "frequency": row["frequency"], "weekdays": [int(d) for d in row["weekdays"] or []],
        "monthDay": row["month_day"], "runTimeIst": row["run_time_ist"].strftime("%H:%M"),
        "expectedCloseMinutes": row["expected_close_minutes"], "assigneeMode": row["assignee_mode"],
        "assignedRole": normalise_role(row["assigned_role_code"]), "assignedActorId": row["assigned_actor_id"],
        "assignedName": row["assigned_display_name"] or names.get(str(row["assigned_actor_id"] or "")),
        "outletId": str(row["outlet_id"]) if row["outlet_id"] else None,
        "startsOn": _iso(row["starts_on"]), "endsOn": _iso(row["ends_on"]),
        "skipDates": [_iso(d) for d in row["skip_dates"] or []], "status": row["schedule_status"],
        "createdByActorId": row["created_by_actor_id"], "createdByRole": normalise_role(row["created_by_role_code"]),
        "createdByName": row["created_by_display_name"] or names.get(str(row["created_by_actor_id"] or "")),
        "createdAtUtc": _iso(row["created_at_utc"]),
    }


def _validate_schedule(connection: Connection, *, tenant_id: str, caller: Caller, spec: dict[str, Any],
                       people: list[dict[str, Any]]) -> dict[str, Any]:
    frequency = str(spec.get("frequency") or "").upper()
    if frequency not in {"DAILY", "WEEKLY", "MONTHLY"}:
        raise TaskManagerError("Choose daily, weekly or monthly.")
    weekdays = sorted({int(d) for d in (spec.get("weekdays") or ([1, 2, 3, 4, 5, 6] if frequency == "DAILY" else []))})
    if any(d < 1 or d > 7 for d in weekdays):
        raise TaskManagerError("Weekdays are 1 (Monday) to 7 (Sunday).")
    if frequency == "DAILY":
        weekdays = [d for d in weekdays if d != 7]
    if frequency in {"DAILY", "WEEKLY"} and not weekdays:
        raise TaskManagerError("Choose at least one weekday (not Sunday for a daily task).")
    month_day = spec.get("monthDay")
    if frequency == "MONTHLY":
        if month_day is None or not 1 <= int(month_day) <= 31:
            raise TaskManagerError("Choose the day of the month (1 to 31).")
        month_day = int(month_day)
    else:
        month_day = None
    minutes = int(spec.get("expectedCloseMinutes") or 0)
    if not 1 <= minutes <= MAX_EXPECTED_MINUTES:
        raise TaskManagerError("Set the expected time to close each occurrence.")
    run_time = str(spec.get("runTimeIst") or "09:00")
    try:
        hh, mm = (int(x) for x in run_time.split(":")[:2])
        run_at = time(hh, mm)
    except ValueError as exc:
        raise TaskManagerError("The time of day is HH:MM.") from exc
    mode = str(spec.get("assigneeMode") or "ACTOR").upper()
    role = normalise_role(spec.get("assignedRole"))
    actor = (spec.get("assignedActorId") or "").strip() or None
    name = None
    if mode == "ACTOR":
        person = _person(people, actor or "")
        if person is None:
            raise TaskManagerError("Choose an active person of this Project.")
        role, name = person["roleCode"], person["displayName"]
    elif mode == "ROLE_EACH":
        if role not in ALL_ROLES:
            raise TaskManagerError("Choose the role whose people get the task.")
        actor = None
    elif mode == "OUTLET_PCS":
        role, actor = "PC", None
    else:
        raise TaskManagerError("Unknown assignee rule.")
    if role not in assignable_roles(caller):
        raise TaskManagerForbidden(f"You cannot assign a task to a {role}.")
    outlet_id = spec.get("outletId") or None
    if outlet_id:
        _outlet(connection, tenant_id=tenant_id, outlet_id=outlet_id)
    title, description = _clean(spec.get("title"), limit=300), _clean(spec.get("description"), limit=4000)
    if not title or not description:
        raise TaskManagerError("A title and a description are required.")
    priority = str(spec.get("priority") or "NORMAL").upper()
    if priority not in {"URGENT", "HIGH", "NORMAL", "LOW"}:
        raise TaskManagerError("Unknown priority.")
    starts_on = date.fromisoformat(spec["startsOn"]) if spec.get("startsOn") else ist_today()
    ends_on = date.fromisoformat(spec["endsOn"]) if spec.get("endsOn") else None
    if ends_on and ends_on < starts_on:
        raise TaskManagerError("The end date is before the start date.")
    return {
        "title": title, "description": description, "priority": priority, "frequency": frequency,
        "weekdays": weekdays, "month_day": month_day, "run_time": run_at, "minutes": minutes, "mode": mode,
        "role": role, "actor": actor, "name": name, "outlet": str(outlet_id) if outlet_id else None,
        "starts_on": starts_on, "ends_on": ends_on,
    }


def create_schedule(connection: Connection, *, tenant_id: str, caller: Caller, spec: dict[str, Any]) -> dict[str, Any]:
    if caller.role not in SCHEDULE_OWNERS:
        raise TaskManagerForbidden("Only an Executive or Superadmin can create recurring tasks.")
    people = project_people(connection, tenant_id=tenant_id)
    v = _validate_schedule(connection, tenant_id=tenant_id, caller=caller, spec=spec, people=people)
    row = connection.execute(
        text(f"""
            INSERT INTO auditcore.p2_task_schedules (
              tenant_id, title, description, priority, frequency, weekdays, month_day, run_time_ist,
              expected_close_minutes, assignee_mode, assigned_role_code, assigned_actor_id, assigned_display_name,
              outlet_id, starts_on, ends_on, created_by_actor_id, created_by_role_code, created_by_display_name
            ) VALUES (
              :t, :title, :description, :priority, :frequency, CAST(:weekdays AS smallint[]), :month_day, :run_time,
              :minutes, :mode, :role, :actor, :name, :outlet, :starts_on, :ends_on, :me, :my_role, :my_name
            )
            RETURNING {_SCHEDULE_FIELDS}
        """),
        {"t": tenant_id, **{k: v[k] for k in ("title", "description", "priority", "frequency", "weekdays",
                                               "month_day", "run_time", "minutes", "mode", "role", "actor", "name",
                                               "outlet", "starts_on", "ends_on")},
         "me": caller.actor_id, "my_role": caller.role, "my_name": caller.display_name},
    ).mappings().one()
    return _public_schedule(dict(row), known_names(connection, tenant_id=tenant_id))


def list_schedules(connection: Connection, *, tenant_id: str, caller: Caller) -> list[dict[str, Any]]:
    if caller.role not in SCHEDULE_OWNERS and caller.role != "PM":
        raise TaskManagerForbidden("Only an Executive, a PM or Superadmin can see recurring tasks.")
    rows = connection.execute(
        text(f"SELECT {_SCHEDULE_FIELDS} FROM auditcore.p2_task_schedules WHERE tenant_id = :t "
             "AND schedule_status <> 'ARCHIVED' ORDER BY system_kind NULLS LAST, created_at_utc DESC"),
        {"t": tenant_id},
    ).mappings().all()
    names = known_names(connection, tenant_id=tenant_id)
    return [_public_schedule(dict(r), names) for r in rows]


def _schedule(connection: Connection, *, tenant_id: str, schedule_id: UUID, lock: bool = False) -> dict[str, Any]:
    row = connection.execute(
        text(f"SELECT {_SCHEDULE_FIELDS} FROM auditcore.p2_task_schedules WHERE tenant_id = :t AND schedule_id = :s"
             + (" FOR UPDATE" if lock else "")),
        {"t": tenant_id, "s": str(schedule_id)},
    ).mappings().one_or_none()
    if row is None:
        raise TaskManagerNotFound("Recurring task not found.")
    return dict(row)


def update_schedule(connection: Connection, *, tenant_id: str, caller: Caller, schedule_id: UUID,
                    spec: dict[str, Any]) -> dict[str, Any]:
    if caller.role not in SCHEDULE_OWNERS:
        raise TaskManagerForbidden("Only an Executive or Superadmin can change recurring tasks.")
    current = _schedule(connection, tenant_id=tenant_id, schedule_id=schedule_id, lock=True)
    status = str(spec.get("status") or current["schedule_status"]).upper()
    if status not in {"ACTIVE", "PAUSED", "ARCHIVED"}:
        raise TaskManagerError("Unknown status.")
    if current["system_kind"]:
        # The gate pass task is the system's: only pausing or resuming it.
        if status == "ARCHIVED":
            raise TaskManagerError("The gate pass task can be paused, not removed.")
        connection.execute(
            text("UPDATE auditcore.p2_task_schedules SET schedule_status = :s, updated_at_utc = now() "
                 "WHERE tenant_id = :t AND schedule_id = :id"),
            {"s": status, "t": tenant_id, "id": str(schedule_id)},
        )
    else:
        merged = {
            "title": current["title"], "description": current["description"], "priority": current["priority"],
            "frequency": current["frequency"], "weekdays": list(current["weekdays"] or []),
            "monthDay": current["month_day"], "runTimeIst": current["run_time_ist"].strftime("%H:%M"),
            "expectedCloseMinutes": current["expected_close_minutes"], "assigneeMode": current["assignee_mode"],
            "assignedRole": current["assigned_role_code"], "assignedActorId": current["assigned_actor_id"],
            "outletId": str(current["outlet_id"]) if current["outlet_id"] else None,
            "startsOn": _iso(current["starts_on"]), "endsOn": _iso(current["ends_on"]),
            **{k: v for k, v in spec.items() if k != "status"},
        }
        people = project_people(connection, tenant_id=tenant_id)
        v = _validate_schedule(connection, tenant_id=tenant_id, caller=caller, spec=merged, people=people)
        connection.execute(
            text("""
                UPDATE auditcore.p2_task_schedules
                SET title = :title, description = :description, priority = :priority, frequency = :frequency,
                    weekdays = CAST(:weekdays AS smallint[]), month_day = :month_day, run_time_ist = :run_time,
                    expected_close_minutes = :minutes, assignee_mode = :mode, assigned_role_code = :role,
                    assigned_actor_id = :actor, assigned_display_name = :name, outlet_id = :outlet,
                    starts_on = :starts_on, ends_on = :ends_on, schedule_status = :status, updated_at_utc = now()
                WHERE tenant_id = :t AND schedule_id = :id
            """),
            {"t": tenant_id, "id": str(schedule_id), "status": status,
             **{k: v[k] for k in ("title", "description", "priority", "frequency", "weekdays", "month_day",
                                  "run_time", "minutes", "mode", "role", "actor", "name", "outlet",
                                  "starts_on", "ends_on")}},
        )
    row = _schedule(connection, tenant_id=tenant_id, schedule_id=schedule_id)
    return _public_schedule(row, known_names(connection, tenant_id=tenant_id))


def skip_occurrence(connection: Connection, *, tenant_id: str, caller: Caller, schedule_id: UUID, day: date,
                    reason: str) -> dict[str, Any]:
    """Skip one date of a recurring task (a holiday): an occurrence already
    raised is cancelled with the reason; a future date is never raised."""
    reason = _clean(reason, limit=1000)
    if not reason:
        raise TaskManagerError("Say why this date is skipped.")
    current = _schedule(connection, tenant_id=tenant_id, schedule_id=schedule_id, lock=True)
    if caller.role not in SCHEDULE_OWNERS and str(current["created_by_actor_id"] or "") != caller.actor_id:
        raise TaskManagerForbidden("Only the owner of the recurring task can skip a date.")
    if day < ist_today():
        raise TaskManagerError("A past date cannot be skipped.")
    open_ids = connection.execute(
        text("""
            SELECT task_id FROM auditcore.p2_tasks
            WHERE tenant_id = :t AND schedule_id = :s AND occurrence_date = :d
              AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
        """),
        {"t": tenant_id, "s": str(schedule_id), "d": day},
    ).scalars().all()
    for task_id in open_ids:
        connection.execute(
            text("UPDATE auditcore.p2_tasks SET task_status = 'CANCELLED', updated_at_utc = now() "
                 "WHERE tenant_id = :t AND task_id = :id"),
            {"t": tenant_id, "id": str(task_id)},
        )
        _event(connection, tenant_id=tenant_id, task_id=task_id, event_type="CANCELLED", caller=caller,
               comment=reason, details={"skippedDate": day.isoformat()})
    connection.execute(
        text("""
            UPDATE auditcore.p2_task_schedules
            SET skip_dates = (SELECT ARRAY(SELECT DISTINCT d FROM unnest(skip_dates || ARRAY[CAST(:d AS date)]) d
                                           ORDER BY d)),
                updated_at_utc = now()
            WHERE tenant_id = :t AND schedule_id = :s
        """),
        {"t": tenant_id, "s": str(schedule_id), "d": day},
    )
    return {"scheduleId": str(schedule_id), "date": day.isoformat(), "cancelledTasks": len(open_ids)}


# --------------------------------------------------------------------------- occurrences


def ist_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(IST).date()


def _monthly_date(year: int, month: int, month_day: int) -> date:
    nominal = date(year, month, min(month_day, calendar.monthrange(year, month)[1]))
    return nominal + timedelta(days=1) if nominal.isoweekday() == 7 else nominal


def occurs_on(schedule: dict[str, Any], day: date) -> bool:
    """Whether a recurring task has an occurrence on ``day``. Never a daily one
    on a Sunday; a weekly or monthly one falling on a Sunday moves to the Monday."""
    if day < schedule["starts_on"] or (schedule["ends_on"] and day > schedule["ends_on"]):
        return False
    if day in set(schedule["skip_dates"] or []):
        return False
    weekday = day.isoweekday()
    days = {int(d) for d in schedule["weekdays"] or []}
    frequency = schedule["frequency"]
    if frequency == "DAILY":
        return weekday != 7 and weekday in days
    if frequency == "WEEKLY":
        if weekday == 7:
            return False
        return weekday in days or (weekday == 1 and 7 in days)
    month_day = int(schedule["month_day"])
    previous = day.replace(day=1) - timedelta(days=1)
    return day in {_monthly_date(day.year, day.month, month_day),
                   _monthly_date(previous.year, previous.month, month_day)}


def _ist_at(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=IST).astimezone(UTC)


def _occurrence_assignees(connection: Connection, *, tenant_id: str, schedule: dict[str, Any]) -> list[dict[str, Any]]:
    """Who gets this occurrence: a person, every person of a role, or one
    task per outlet for that outlet's PCs."""
    people = project_people(connection, tenant_id=tenant_id)
    outlet = str(schedule["outlet_id"]) if schedule["outlet_id"] else None
    mode = schedule["assignee_mode"]
    if mode == "ACTOR":
        person = _person(people, str(schedule["assigned_actor_id"]))
        if person is None:
            logger.info("tm_schedule_assignee_inactive", tenant_id=tenant_id,
                        schedule_id=str(schedule["schedule_id"]))
            return []
        return [{"key": person["actorId"], "actor": person["actorId"], "role": person["roleCode"],
                 "name": person["displayName"], "outlet": None}]
    if mode == "ROLE_EACH":
        role = normalise_role(schedule["assigned_role_code"])
        chosen = [p for p in people if p["roleCode"] == role
                  and (outlet is None or p["projectWide"] or any(o["outletId"] == outlet for o in p["outlets"]))]
        return [{"key": p["actorId"], "actor": p["actorId"], "role": role, "name": p["displayName"], "outlet": None}
                for p in chosen]
    rows = connection.execute(
        text(f"""
            SELECT DISTINCT o.dealer_id, o.outlet_id, o.outlet_name
            FROM auditcore.business_assignments ba
            JOIN auditcore.dealer_outlets o ON o.tenant_id = ba.tenant_id AND o.outlet_id = ba.outlet_id
            WHERE ba.tenant_id = :t AND {_ACTIVE} AND upper(ba.business_role_code) = 'PC'
              AND (CAST(:outlet AS uuid) IS NULL OR o.outlet_id = CAST(:outlet AS uuid))
            ORDER BY o.outlet_name
        """),
        {"t": tenant_id, "outlet": outlet},
    ).mappings().all()
    return [{"key": str(r["outlet_id"]), "actor": None, "role": "PC", "name": None,
             "outlet": {"dealer_id": str(r["dealer_id"]), "outlet_id": str(r["outlet_id"]),
                        "outlet_name": r["outlet_name"]}} for r in rows]


def generate_occurrence(connection: Connection, *, tenant_id: str, schedule: dict[str, Any], day: date,
                        now: datetime | None = None, claimed_by: str | None = None) -> int:
    """Raise the occurrence of ``day`` once (claimed in p2_scheduled_runs).
    Earlier occurrences still open for the same assignee are marked Missed."""
    now = now or datetime.now(UTC)
    schedule_id = str(schedule["schedule_id"])
    if not claim_scheduled_run(connection, tenant_id=tenant_id, run_name=f"TM_OCCURRENCE:{schedule_id}",
                               run_key=day.isoformat(), claimed_by=claimed_by):
        return 0
    gate_pass = schedule["system_kind"] == GATE_PASS_SYSTEM_KIND
    raised = 0
    for who in _occurrence_assignees(connection, tenant_id=tenant_id, schedule=schedule):
        # A gate pass task of day N is due at the end of N+1 and is marked
        # incomplete then, so the next day's task never marks it Missed.
        connection.execute(
            text("""
                UPDATE auditcore.p2_tasks SET missed_at_utc = :now, updated_at_utc = :now
                WHERE tenant_id = :t AND schedule_id = :s AND occurrence_date < :d AND missed_at_utc IS NULL
                  AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
                  AND (assigned_actor_id = :key OR outlet_id::text = :key)
                  AND task_type <> :gate
            """),
            {"t": tenant_id, "s": schedule_id, "d": day, "key": who["key"], "now": now, "gate": GATE_PASS_TASK},
        )
        if gate_pass:
            protocol, reviewer_role = "SYSTEM_VERIFIED", None
            due = _ist_at(day + timedelta(days=1), time(23, 59, 59))
            title = f"Daily gate pass: {who['outlet']['outlet_name']}, {day:%d-%m-%y}"
            description = (
                f"Photograph the gate pass of {who['outlet']['outlet_name']} for {day:%d-%m-%Y} and enter its "
                "counts on the Daily Gate Pass screen. The system closes this task when the gate pass is "
                "finished; if it is not finished by 23:59 tomorrow it is marked incomplete."
            )
        else:
            protocol, reviewer_role = _reviewer_for_recurring(connection, tenant_id=tenant_id,
                                                              assignee_role=who["role"])
            due = _ist_at(day, schedule["run_time_ist"]) + timedelta(minutes=int(schedule["expected_close_minutes"]))
            title, description = schedule["title"], schedule["description"]
        outlet = who["outlet"]
        task_id = _insert_task(connection, tenant_id=tenant_id, values={
            "scope": "OUTLET" if outlet else "PROJECT",
            "dealer_id": outlet["dealer_id"] if outlet else None,
            "outlet_id": outlet["outlet_id"] if outlet else None,
            "task_type": GATE_PASS_TASK if gate_pass else RECURRING_TASK,
            "category": "DAILY_GATE_PASS" if gate_pass else (schedule["category"] or "RECURRING"),
            "origin": "SYSTEM", "source_type": "SCHEDULE", "source_code": schedule_id,
            "dedupe_key": f"sched:{schedule_id}:{day.isoformat()}:{who['key']}",
            "title": title, "description": description, "priority": schedule["priority"],
            "reference": {"outletName": outlet["outlet_name"], "businessDate": day.isoformat()} if outlet
                         else {"occurrenceDate": day.isoformat()},
            "assigned_role": who["role"], "assigned_actor": who["actor"], "assigned_name": who["name"],
            "raised_by": schedule["created_by_actor_id"], "raised_role": schedule["created_by_role_code"],
            "raised_name": schedule["created_by_display_name"],
            "reviewer_role": reviewer_role, "reviewer_actor": None, "protocol": protocol,
            "expected_close_minutes": int(schedule["expected_close_minutes"]), "due": due,
            "schedule_id": schedule_id, "occurrence_date": day,
        })
        if task_id is not None:
            _event(connection, tenant_id=tenant_id, task_id=task_id, event_type="RAISED", caller=None,
                   details={"scheduleId": schedule_id, "occurrenceDate": day.isoformat()})
            raised += 1
    return raised


def ensure_system_schedules(connection: Connection, *, tenant_id: str, now: datetime | None = None) -> None:
    """The Daily Gate Pass schedule of the Project (one per Project). A newly
    seeded schedule starts the next day: a task raised late in the evening
    for a day nobody was told about would only be noise."""
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_task_schedules (
              tenant_id, system_kind, title, description, category, priority, frequency, weekdays,
              run_time_ist, expected_close_minutes, assignee_mode, assigned_role_code, starts_on,
              created_by_actor_id, created_by_role_code, created_by_display_name
            ) VALUES (
              :t, :kind, 'Daily gate pass', 'Photograph the outlet''s gate pass of the day and enter its counts.',
              'DAILY_GATE_PASS', 'HIGH', 'DAILY', ARRAY[1,2,3,4,5,6]::smallint[], '07:00', 2340,
              'OUTLET_PCS', 'PC', :starts, 'SYSTEM', 'SYSTEM', 'System'
            )
            ON CONFLICT (tenant_id, system_kind) WHERE system_kind IS NOT NULL DO NOTHING
        """),
        {"t": tenant_id, "kind": GATE_PASS_SYSTEM_KIND, "starts": ist_today(now) + timedelta(days=1)},
    )


def close_gate_pass_tasks(connection: Connection, *, tenant_id: str, outlet_id: str | UUID | None = None,
                          business_date: date | None = None) -> int:
    """Close the gate pass task of every outlet and day whose ACTIVE gate pass
    is finished (photo and counts, any explanation given)."""
    rows = connection.execute(
        text("""
            UPDATE auditcore.p2_tasks t
            SET task_status = 'VERIFIED_COMPLETE', verified_at_utc = g.completed_at_utc,
                action_completed_at_utc = g.completed_at_utc, completed_by_actor_id = g.pc_actor_id,
                completion_result = t.completion_result
                    || jsonb_build_object('outcome', 'DONE', 'gatePassId', g.gate_pass_id::text),
                updated_at_utc = now()
            FROM auditcore.p2_gate_passes g
            WHERE t.tenant_id = :t AND t.task_type = :type AND t.task_status IN ('READY','IN_PROGRESS')
              AND g.tenant_id = t.tenant_id AND g.outlet_id = t.outlet_id AND g.business_date = t.occurrence_date
              AND g.status = 'ACTIVE' AND g.completed_at_utc IS NOT NULL
              AND (CAST(:outlet AS uuid) IS NULL OR t.outlet_id = CAST(:outlet AS uuid))
              AND (CAST(:day AS date) IS NULL OR t.occurrence_date = CAST(:day AS date))
            RETURNING t.task_id, g.gate_pass_id
        """),
        {"t": tenant_id, "type": GATE_PASS_TASK, "outlet": str(outlet_id) if outlet_id else None,
         "day": business_date},
    ).mappings().all()
    for row in rows:
        _event(connection, tenant_id=tenant_id, task_id=row["task_id"], event_type="COMPLETED", caller=None,
               details={"gatePassId": str(row["gate_pass_id"]), "by": "SYSTEM"})
    return len(rows)


def fail_unfinished_gate_passes(connection: Connection, *, tenant_id: str, now: datetime | None = None) -> int:
    """A gate pass task not finished by 23:59 IST of the next day is incomplete."""
    now = now or datetime.now(UTC)
    rows = connection.execute(
        text("""
            UPDATE auditcore.p2_tasks t
            SET task_status = 'FAILED', missed_at_utc = COALESCE(t.missed_at_utc, :now), updated_at_utc = :now,
                completion_result = t.completion_result || jsonb_build_object(
                  'outcome', 'INCOMPLETE',
                  'reason', CASE WHEN EXISTS (
                      SELECT 1 FROM auditcore.p2_gate_passes g
                      WHERE g.tenant_id = t.tenant_id AND g.outlet_id = t.outlet_id
                        AND g.business_date = t.occurrence_date AND g.status = 'ACTIVE')
                    THEN 'The gate pass was uploaded but its counts were not finished.'
                    ELSE 'No gate pass was uploaded for the day.' END)
            WHERE t.tenant_id = :t AND t.task_type = :type AND t.task_status IN ('READY','IN_PROGRESS')
              AND t.due_at_utc < :now
            RETURNING t.task_id
        """),
        {"t": tenant_id, "type": GATE_PASS_TASK, "now": now},
    ).scalars().all()
    for task_id in rows:
        _event(connection, tenant_id=tenant_id, task_id=task_id, event_type="INCOMPLETE", caller=None)
    return len(rows)


def run_tick(connection: Connection, *, tenant_id: str, now: datetime | None = None,
             claimed_by: str | None = None) -> dict[str, int]:
    """The worker's Task Manager pass for one Project: seed the gate pass
    schedule, raise today's due occurrences, settle gate pass tasks."""
    now = now or datetime.now(UTC)
    ensure_system_schedules(connection, tenant_id=tenant_id, now=now)
    today = ist_today(now)
    now_ist = now.astimezone(IST).time()
    schedules = connection.execute(
        text(f"SELECT {_SCHEDULE_FIELDS} FROM auditcore.p2_task_schedules "
             "WHERE tenant_id = :t AND schedule_status = 'ACTIVE'"),
        {"t": tenant_id},
    ).mappings().all()
    raised = 0
    for schedule in schedules:
        schedule = dict(schedule)
        if now_ist >= schedule["run_time_ist"] and occurs_on(schedule, today):
            raised += generate_occurrence(connection, tenant_id=tenant_id, schedule=schedule, day=today, now=now,
                                          claimed_by=claimed_by)
    closed = close_gate_pass_tasks(connection, tenant_id=tenant_id)
    failed = fail_unfinished_gate_passes(connection, tenant_id=tenant_id, now=now)
    return {"raised": raised, "gatePassClosed": closed, "gatePassIncomplete": failed}
