"""PC Home and the Tracker tab for PCs and Reviewers (product owner, 2026-11-02).

``GET /p2/v1/tenants/{t}/my-work?from=&to=`` answers, for the signed-in PC or
Reviewer and a day or a week (up to 31 days), per outlet they are mapped to:

PC
    journeysTotal       every Journey they ever opened at the outlet
    journeysUploaded    Journeys they uploaded a file to in the period
    bookingsUploaded    of those, Journeys uploaded to while still at booking
    deliveriesUpdated   of those, Journeys with a delivery page in those uploads
    gateRegister        per gate register day of the period: uploaded or not, total
                        on the register, intimated to the PC, uploaded in the tool,
                        pending with the PC and not intimated
    pendingDeliveryFiles  the period's "pending with PC" summed
    tasksWorked         tasks they acted on in the period (a task counts once)
    tasksPending        tasks open for them now

Reviewer
    journeysWorked      Journeys they marked reviewed, corrected or worked a task on
    reviewsClosed       Booking / Delivery reviewed marks they gave
    tasksClosed         tasks they closed, the Team Lead tasks they now close too
                        (task_closure_roles)
    tasksPending        tasks open for them now (their own and the outlet's Team Lead tasks)

``GET .../my-work/checkout-check``: the gate registers of the previous gate register
day (N-1; Monday's N-1 is Saturday) the PC has not finished at their ONSITE outlets.
The web shows it as a warning before check-out; it never blocks check-out.

Every figure is read from what Audit Core already stores; nothing is written here.
The gate register figures use the gate pass module's own rule (uc03_p2_gate_pass:
a delivery counts when its vehicle gate pass, dated that day, is on the Journey) so
this page, the PC's Daily Gate Pass, the calendar and the TL's consolidated view
always agree. Day boundaries are the Project's timezone (IST for the gate register).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.task_closure_roles import roles_closable_by
from audit_core.task_manager import (
    caller_role,
    gate_pass_day_label,
    gate_pass_due,
    previous_gate_pass_day,
)
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_gate_pass import scope_outlets, vehicle_gate_pass_deliveries
from audit_core.uc03_p2_object_keys import ist_day

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-my-work"])

_READ_PERMISSION = "audit.journey.read"
_FALLBACK_TIMEZONE = "Asia/Kolkata"
MAX_DAYS = 31

# What a person does on a task (as the Daily Processing report counts work).
_WORK_EVENTS = (
    "UPLOAD_DOCUMENT", "REUPLOAD_DOCUMENT", "REVIEW_DOCUMENT", "CORRECT_EXTRACTED_FIELD", "ADD_EVIDENCE",
    "ADD_COMMENT", "PROVIDE_FEEDBACK", "REQUEST_CLARIFICATION", "COMPLETE_ACTION", "APPROVE_CORRECTION",
    "REJECT_CORRECTION", "ACCEPT", "REJECT", "ACCEPT_EXCEPTION", "PROVIDE_VEHICLE_ID", "CONFIRM_BREACH",
    "MARK_FALSE_POSITIVE", "CLOSE_TASK", "COMPLETED", "VERDICT", "STARTED",
)
# The events by which a person closes a task.
_CLOSING_EVENTS = (
    "COMPLETE_ACTION", "CLOSE_TASK", "ACCEPT", "ACCEPT_EXCEPTION", "CONFIRM_BREACH", "MARK_FALSE_POSITIVE",
    "APPROVE_CORRECTION", "REJECT_CORRECTION", "VERDICT", "COMPLETED",
)
_OPEN_SQL = "t.task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')"


def _timezone(connection: Connection, tenant_id: str) -> str:
    row = connection.execute(
        text("SELECT timezone_name FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant_id},
    ).scalar_one_or_none()
    return str(row or _FALLBACK_TIMEZONE)


def _period(start: date | None, end: date | None) -> tuple[date, date]:
    today = ist_day().date()
    end = min(end or today, today)
    start = start or end
    if start > end or (end - start).days >= MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"Choose a range of 1 to {MAX_DAYS} days.")
    return start, end


def _my_outlets(connection: Connection, tenant_id: str, actor_id: str, role: str) -> list[dict[str, Any]]:
    """The outlets the caller holds the role at (a Reviewer's dealer-wide mapping covers every outlet)."""
    return [o for o in scope_outlets(connection, tenant_id, actor_id)
            if role in {str(r).upper() for r in (o.get("roles") or [])}]


def gate_register_days(start: date, end: date) -> list[date]:
    """The gate register days in the period: every day but Sunday (Monday's covers it)."""
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).isoweekday() != 7]


def gate_register(connection: Connection, tenant_id: str, outlets: list[dict[str, Any]], start: date, end: date,
                  *, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """Per outlet, per gate register day: what the register says against the tool.

    pendingWithPc = intimated - uploaded; notIntimated = total - intimated. A gate pass
    entered before 2 Nov (no intimated count) is read as the consolidated view reads it:
    the PC's later answer, or the whole gap with the PC while unanswered."""
    now = now or datetime.now(UTC)
    days = gate_register_days(start, end)
    ids = [str(o["outlet_id"]) for o in outlets]
    out: dict[str, list[dict[str, Any]]] = {oid: [] for oid in ids}
    if not ids or not days:
        return out
    passes = {(r["business_date"], str(r["outlet_id"])): dict(r) for r in connection.execute(
        text("""
            SELECT business_date, outlet_id, gate_pass_id, new_deliveries, not_notified_count, files_held,
                   counts_submitted_at_utc, completed_at_utc
            FROM auditcore.p2_gate_passes
            WHERE tenant_id = :t AND status = 'ACTIVE' AND outlet_id = ANY(CAST(:ids AS uuid[]))
              AND business_date BETWEEN :a AND :b
        """), {"t": tenant_id, "ids": ids, "a": start, "b": end}).mappings().all()}
    uploaded = vehicle_gate_pass_deliveries(connection, tenant_id, ids, days[0], days[-1])
    for oid in ids:
        for d in days:
            gp = passes.get((d, oid))
            files = uploaded.get((d, oid), 0)
            entry: dict[str, Any] = {
                "date": d.isoformat(), "label": gate_pass_day_label(d), "dueAtUtc": gate_pass_due(d).isoformat(),
                "uploadedInTool": files, "total": None, "intimated": None, "pendingWithPc": None,
                "notIntimated": None, "gatePassId": None,
            }
            if gp is None:
                entry["status"] = "MISSED" if now > gate_pass_due(d) else "NOT_UPLOADED"
            else:
                entry["gatePassId"] = str(gp["gate_pass_id"])
                if gp["counts_submitted_at_utc"] is None:
                    entry["status"] = "COUNTS_PENDING"
                else:
                    total = int(gp["new_deliveries"] or 0)
                    gap = max(total - files, 0)
                    not_notified = gp["not_notified_count"]
                    if not_notified is None:
                        not_notified = gap if gp["files_held"] is False else 0
                    # Not intimated as the PC entered it; the pending part is what of the
                    # intimated deliveries is not in the tool yet (live, not as at entry).
                    not_intimated = min(int(not_notified), total)
                    entry.update(total=total, intimated=total - not_intimated, notIntimated=not_intimated,
                                 pendingWithPc=max(total - not_intimated - files, 0),
                                 status="DONE" if gp["completed_at_utc"] is not None else "ANSWER_PENDING")
            out[oid].append(entry)
    return out


def _uploads(connection: Connection, *, tenant_id: str, actor_id: str, tz: str, start: date,
             end: date) -> list[dict[str, Any]]:
    """Journeys the caller uploaded to in the period, per outlet, and whether any page of those
    uploads was a delivery page (p2_document_queue.business_stage = 'DELIVERY'). The page's own stage
    is used, not the Journey's stage trail: on live data most Journeys never recorded a stage change,
    which counted 1 delivery where the PC had uploaded 73 (checked 2026-11-02). A Journey uploaded to
    with no delivery page counts as a booking upload."""
    return [dict(r) for r in connection.execute(
        text(
            """
            WITH b AS (
                SELECT (CAST(:a AS date))::timestamp AT TIME ZONE :tz AS s,
                       (CAST(:b AS date) + 1)::timestamp AT TIME ZONE :tz AS e
            )
            SELECT ub.journey_id, j.outlet_id,
                   COALESCE(bool_or(q.business_stage = 'DELIVERY'), false) AS delivery
            FROM auditcore.p2_upload_batches ub
            CROSS JOIN b
            JOIN auditcore.journeys j ON j.tenant_id=ub.tenant_id AND j.journey_id=ub.journey_id
            LEFT JOIN auditcore.p2_document_queue q ON q.tenant_id=ub.tenant_id AND q.batch_id=ub.batch_id
            WHERE ub.tenant_id=:t AND ub.uploaded_by_actor_id=:me
              AND ub.created_at_utc >= b.s AND ub.created_at_utc < b.e
              AND ub.batch_status <> 'AWAITING_UPLOAD'
              AND NOT (ub.batch_status='CANCELLED' AND ub.page_count=0)
            GROUP BY ub.journey_id, j.outlet_id
            """
        ),
        {"t": tenant_id, "me": actor_id, "tz": tz, "a": start, "b": end},
    ).mappings().all()]


def _journeys_total(connection: Connection, *, tenant_id: str, actor_id: str) -> dict[str, int]:
    """Every Journey the PC opened or uploaded a file to, ever, per outlet."""
    return {str(r["outlet_id"]): int(r["n"]) for r in connection.execute(
        text("""
            SELECT j.outlet_id, count(*) AS n FROM auditcore.journeys j
            WHERE j.tenant_id = :t
              AND (j.created_by_actor_id = :me
                   OR EXISTS (SELECT 1 FROM auditcore.p2_upload_batches ub
                              WHERE ub.tenant_id = j.tenant_id AND ub.journey_id = j.journey_id
                                AND ub.uploaded_by_actor_id = :me AND ub.batch_status <> 'AWAITING_UPLOAD'))
            GROUP BY j.outlet_id
        """),
        {"t": tenant_id, "me": actor_id},
    ).mappings().all()}


def _tasks_pending(connection: Connection, *, tenant_id: str, actor_id: str, role: str,
                   outlet_ids: list[str]) -> dict[str, int]:
    """Open tasks the caller is expected to work now, per outlet: assigned to them, or
    to a role whose tasks they close (task_closure_roles) at an outlet they cover."""
    if not outlet_ids:
        return {}
    return {str(r["outlet_id"]): int(r["n"]) for r in connection.execute(
        text(
            f"""
            SELECT COALESCE(t.outlet_id, j.outlet_id) AS outlet_id, count(*) AS n
            FROM auditcore.p2_tasks t
            LEFT JOIN auditcore.journeys j ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
            WHERE t.tenant_id = :t AND {_OPEN_SQL}
              AND (t.assigned_actor_id = :me
                   OR (t.assigned_actor_id IS NULL AND upper(t.assigned_role_code) = ANY(CAST(:roles AS text[]))
                       -- A PC's Journey tasks are those of the Journeys they opened; another PC's
                       -- bookings at the same outlet are not theirs to clear.
                       AND (:role <> 'PC' OR t.journey_id IS NULL OR j.created_by_actor_id = :me)))
              AND COALESCE(t.outlet_id, j.outlet_id) = ANY(CAST(:ids AS uuid[]))
            GROUP BY 1
            """
        ),
        {"t": tenant_id, "me": actor_id, "roles": roles_closable_by(role), "ids": outlet_ids, "role": role},
    ).mappings().all()}


def _task_events(connection: Connection, *, tenant_id: str, actor_id: str, tz: str, start: date,
                 end: date) -> list[dict[str, Any]]:
    """Every task the caller worked in the period (one row per task), with its outlet and
    whether the caller closed it in the period."""
    return [dict(r) for r in connection.execute(
        text(
            """
            WITH b AS (
                SELECT (CAST(:a AS date))::timestamp AT TIME ZONE :tz AS s,
                       (CAST(:b AS date) + 1)::timestamp AT TIME ZONE :tz AS e
            )
            SELECT t.task_id, t.journey_id, COALESCE(t.outlet_id, j.outlet_id) AS outlet_id,
                   bool_or(te.event_type = ANY(:closing) AND t.task_status = 'VERIFIED_COMPLETE') AS closed
            FROM auditcore.p2_task_events te
            CROSS JOIN b
            JOIN auditcore.p2_tasks t ON t.tenant_id = te.tenant_id AND t.task_id = te.task_id
            LEFT JOIN auditcore.journeys j ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
            WHERE te.tenant_id = :t AND te.actor_id = :me AND te.event_type = ANY(:work)
              AND te.created_at_utc >= b.s AND te.created_at_utc < b.e
            GROUP BY t.task_id, t.journey_id, COALESCE(t.outlet_id, j.outlet_id)
            UNION
            SELECT t.task_id, t.journey_id, COALESCE(t.outlet_id, j.outlet_id), true
            FROM auditcore.p2_tasks t
            CROSS JOIN b
            LEFT JOIN auditcore.journeys j ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
            WHERE t.tenant_id = :t AND t.completed_by_actor_id = :me AND t.task_status = 'VERIFIED_COMPLETE'
              AND COALESCE(t.action_completed_at_utc, t.verified_at_utc) >= b.s
              AND COALESCE(t.action_completed_at_utc, t.verified_at_utc) < b.e
            """
        ),
        {"t": tenant_id, "me": actor_id, "tz": tz, "a": start, "b": end,
         "work": list(_WORK_EVENTS), "closing": list(_CLOSING_EVENTS)},
    ).mappings().all()]


def _reviews(connection: Connection, *, tenant_id: str, actor_id: str, tz: str, start: date,
             end: date) -> list[dict[str, Any]]:
    """The Reviewer's reviewed marks and corrections in the period, one row per Journey touch."""
    return [dict(r) for r in connection.execute(
        text(
            """
            WITH b AS (
                SELECT (CAST(:a AS date))::timestamp AT TIME ZONE :tz AS s,
                       (CAST(:b AS date) + 1)::timestamp AT TIME ZONE :tz AS e
            )
            SELECT r.journey_id, r.outlet_id, 'REVIEW' AS kind
            FROM auditcore.p2_stage_document_reviews r CROSS JOIN b
            WHERE r.tenant_id = :t AND r.reviewed_by_actor_id = :me
              AND r.reviewed_at_utc >= b.s AND r.reviewed_at_utc < b.e
            UNION ALL
            SELECT DISTINCT l.journey_id, j.outlet_id, 'CORRECTION'
            FROM auditcore.journey_field_change_log l CROSS JOIN b
            JOIN auditcore.journeys j ON j.tenant_id = l.tenant_id AND j.journey_id = l.journey_id
            WHERE l.tenant_id = :t AND l.actor_id = :me AND l.change_reason = 'CORRECTION'
              AND l.changed_at_utc >= b.s AND l.changed_at_utc < b.e
            """
        ),
        {"t": tenant_id, "me": actor_id, "tz": tz, "a": start, "b": end},
    ).mappings().all()]


def _outlet_line(o: dict[str, Any]) -> dict[str, Any]:
    return {"outletId": str(o["outlet_id"]), "outletName": o["outlet_name"], "dealerId": str(o["dealer_id"]),
            "dealerName": o["dealer_name"], "classification": o["outlet_classification"]}


_PC_KEYS = ("journeysTotal", "journeysUploaded", "bookingsUploaded", "deliveriesUpdated", "gateRegisterDays",
            "gateRegisterUploaded", "gateRegisterMissed", "registerTotal", "registerIntimated",
            "uploadedInTool", "pendingDeliveryFiles", "notIntimated", "tasksWorked", "tasksPending")
_REVIEWER_KEYS = ("journeysWorked", "reviewsClosed", "corrections", "tasksWorked", "tasksClosed", "tasksPending")


def build_pc_work(connection: Connection, *, tenant_id: str, actor_id: str, start: date, end: date,
                  now: datetime | None = None) -> dict[str, Any]:
    tz = _timezone(connection, tenant_id)
    outlets = _my_outlets(connection, tenant_id, actor_id, "PC")
    ids = [str(o["outlet_id"]) for o in outlets]
    totals_by_outlet = _journeys_total(connection, tenant_id=tenant_id, actor_id=actor_id)
    uploads = _uploads(connection, tenant_id=tenant_id, actor_id=actor_id, tz=tz, start=start, end=end)
    register = gate_register(connection, tenant_id, outlets, start, end, now=now)
    pending = _tasks_pending(connection, tenant_id=tenant_id, actor_id=actor_id, role="PC", outlet_ids=ids)
    worked = _task_events(connection, tenant_id=tenant_id, actor_id=actor_id, tz=tz, start=start, end=end)
    rows = []
    totals = dict.fromkeys(_PC_KEYS, 0)
    for o in outlets:
        oid = str(o["outlet_id"])
        mine = [u for u in uploads if str(u["outlet_id"]) == oid]
        days = register.get(oid, [])
        done = [d for d in days if d["status"] != "NOT_UPLOADED" and d["status"] != "MISSED"]
        line = {
            **_outlet_line(o),
            "journeysTotal": totals_by_outlet.get(oid, 0),
            "journeysUploaded": len({u["journey_id"] for u in mine}),
            "bookingsUploaded": len({u["journey_id"] for u in mine if not u["delivery"]}),
            "deliveriesUpdated": len({u["journey_id"] for u in mine if u["delivery"]}),
            "gateRegisterDays": len(days),
            "gateRegisterUploaded": len(done),
            "gateRegisterMissed": sum(1 for d in days if d["status"] == "MISSED"),
            "registerTotal": sum(d["total"] or 0 for d in days),
            "registerIntimated": sum(d["intimated"] or 0 for d in days),
            "uploadedInTool": sum(d["uploadedInTool"] for d in days),
            "pendingDeliveryFiles": sum(d["pendingWithPc"] or 0 for d in days),
            "notIntimated": sum(d["notIntimated"] or 0 for d in days),
            "tasksWorked": len({w["task_id"] for w in worked if str(w["outlet_id"]) == oid}),
            "tasksPending": pending.get(oid, 0),
            "gateRegister": days,
        }
        for k in _PC_KEYS:
            totals[k] += line[k]
        rows.append(line)
    # A journey uploaded to at two outlets is impossible, so per-outlet sums are exact;
    # tasks of no outlet (Project-wide) still count in the total.
    totals["tasksWorked"] = len({w["task_id"] for w in worked})
    return {"role": "PC", "outlets": rows, "totals": totals}


def build_reviewer_work(connection: Connection, *, tenant_id: str, actor_id: str, start: date,
                        end: date) -> dict[str, Any]:
    tz = _timezone(connection, tenant_id)
    outlets = _my_outlets(connection, tenant_id, actor_id, "REVIEWER")
    ids = [str(o["outlet_id"]) for o in outlets]
    reviews = _reviews(connection, tenant_id=tenant_id, actor_id=actor_id, tz=tz, start=start, end=end)
    worked = _task_events(connection, tenant_id=tenant_id, actor_id=actor_id, tz=tz, start=start, end=end)
    pending = _tasks_pending(connection, tenant_id=tenant_id, actor_id=actor_id, role="REVIEWER", outlet_ids=ids)
    rows = []
    totals = dict.fromkeys(_REVIEWER_KEYS, 0)
    all_journeys: set[str] = set()
    for o in outlets:
        oid = str(o["outlet_id"])
        marks = [r for r in reviews if str(r["outlet_id"]) == oid]
        tasks = [w for w in worked if str(w["outlet_id"]) == oid]
        journeys = {str(r["journey_id"]) for r in marks} | {str(w["journey_id"]) for w in tasks if w["journey_id"]}
        all_journeys |= journeys
        line = {
            **_outlet_line(o),
            "journeysWorked": len(journeys),
            "reviewsClosed": sum(1 for r in marks if r["kind"] == "REVIEW"),
            "corrections": len({str(r["journey_id"]) for r in marks if r["kind"] == "CORRECTION"}),
            "tasksWorked": len({w["task_id"] for w in tasks}),
            "tasksClosed": len({w["task_id"] for w in tasks if w["closed"]}),
            "tasksPending": pending.get(oid, 0),
        }
        for k in _REVIEWER_KEYS:
            totals[k] += line[k]
        rows.append(line)
    totals["journeysWorked"] = len(all_journeys)
    return {"role": "REVIEWER", "outlets": rows, "totals": totals}


@router.get("/my-work")
def my_work(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                 authorization_client=client, permission_key=_READ_PERMISSION)
    start, end = _period(start, end)
    role = caller_role(connection, tenant_id=tenant_id, actor_id=principal.subject)
    if role == "PC":
        body = build_pc_work(connection, tenant_id=tenant_id, actor_id=principal.subject, start=start, end=end)
    elif role == "REVIEWER":
        body = build_reviewer_work(connection, tenant_id=tenant_id, actor_id=principal.subject, start=start, end=end)
    else:
        raise HTTPException(status_code=404, detail="My work is for a PC or a Reviewer.")
    return {"from": start.isoformat(), "to": end.isoformat(), **body}


def checkout_pending(connection: Connection, *, tenant_id: str, actor_id: str,
                     now: datetime | None = None) -> dict[str, Any]:
    """The previous gate register day's registers not finished at the PC's ONSITE outlets."""
    today = ist_day(now).date()
    day = previous_gate_pass_day(today)
    outlets = [o for o in _my_outlets(connection, tenant_id, actor_id, "PC")
               if o["outlet_classification"] == "ONSITE"]
    if not outlets:
        return {"day": day.isoformat(), "label": gate_pass_day_label(day), "pending": []}
    finished = {str(r) for r in connection.execute(
        text("SELECT outlet_id FROM auditcore.p2_gate_passes WHERE tenant_id = :t AND business_date = :d "
             "AND status = 'ACTIVE' AND completed_at_utc IS NOT NULL AND outlet_id = ANY(CAST(:ids AS uuid[]))"),
        {"t": tenant_id, "d": day, "ids": [str(o["outlet_id"]) for o in outlets]},
    ).scalars().all()}
    return {
        "day": day.isoformat(), "label": gate_pass_day_label(day),
        "pending": [{"outletId": str(o["outlet_id"]), "outletName": o["outlet_name"],
                     "dealerName": o["dealer_name"]}
                    for o in outlets if str(o["outlet_id"]) not in finished],
    }


@router.get("/my-work/checkout-check")
def checkout_check(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Before check-out: the N-1 gate registers the PC has not finished at their onsite
    outlets (product owner 2026-11-02). A warning only; check-out goes ahead on OK."""
    authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                 authorization_client=client, permission_key=_READ_PERMISSION)
    return checkout_pending(connection, tenant_id=tenant_id, actor_id=principal.subject)
