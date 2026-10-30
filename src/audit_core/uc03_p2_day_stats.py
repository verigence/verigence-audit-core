"""Day stats for the Employee (PC) and Team Lead dashboards (product owner, 2026-10-30).

One read, ``GET /p2/v1/tenants/{t}/reports/day-stats?from=&to=``, answers what the
dashboards show for a day (or a range of up to 31 days): how many files were
uploaded and whether each was a Booking or a Delivery upload, how many tasks
closed (Journey Tasks and Task Manager tasks apart), whether the daily gate pass
was uploaded, and the deliveries-versus-gate-pass block. Every figure is read
from what Audit Core already stores -- nothing new is captured:

- uploads: ``p2_upload_batches`` of the range (a cancelled duplicate is not an
  upload). A file is a Delivery upload when the Journey was already in delivery
  when the file came in -- its stage had reached BOOKING_COMPLETE or a DELIVERY
  stage (the STAGE_CHANGED trail in ``p2_activity_events``, or the DELIVERY
  stage record of a Phase 1 journey) before the batch was created; otherwise it
  is a Booking upload. This is the same "in delivery" reading the Booking &
  Delivery screen's chip uses;
- tasks closed: ``p2_tasks`` whose action was completed (or, for a
  machine-verified task, verified) in the range, counted for the person the
  Daily Status counts them for (the assignee; for a PC task the Journey's PC),
  split by ``scope_kind`` (Journey / Task Manager), with closed-late from the
  due time;
- daily gate pass and the deliveries block: ``build_gate_pass_summary`` of the
  gate pass module, so As per register / Updated in tool / Not notified to PC /
  Pending due to PC are the same numbers the consolidated gate pass view shows;
- backlog cleared: delivery Journeys whose vehicle gate pass was uploaded in the
  range but is dated before the day it was uploaded (yesterday's or older
  deliveries caught up today). Counted apart so it never inflates Updated in tool;
- the caller's own line: the above for their own uploads and tasks, plus the
  Task Manager tasks they reviewed and the Journey tasks they worked that closed
  (as the Daily Processing "work done" counts them).

A PC sees their own uploads and tasks and the gate pass figures of their outlets.
A TL, PM or Executive (or an admin) sees every PC, outlet and dealership in
scope, aggregated per outlet and per dealership. Day boundaries are the
Project's timezone; the gate pass figures keep the gate pass module's IST days.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
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
from audit_core.task_manager import known_names
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_gate_pass import (
    MAX_SUMMARY_DAYS,
    build_gate_pass_summary,
    scope_outlets,
)
from audit_core.uc03_p2_object_keys import ist_day

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-day-stats"])

_READ_PERMISSION = "audit.journey.read"
_SUPERVISOR_ROLES = ("TL", "PM", "EXECUTIVE")
_ADMIN_ROLES = ("SUPER_ADMIN", "TENANT_ADMIN")
_FALLBACK_TIMEZONE = "Asia/Kolkata"

# The stages at which a Journey is in delivery (journeys:summary's "in delivery" chip).
_DELIVERY_STAGES = ("BOOKING_COMPLETE", "DELIVERY_DOCUMENT_UPLOAD", "DELIVERY_VERIFY_DOCUMENTS", "DELIVERY_COMPLETE")

# The task roles the Daily Status counts (a machine task is nobody's work).
_TASK_ROLES = ("PC", "TL", "PM", "EXECUTIVE", "CRM", "REVIEWER")

# What a person does on a Journey task (as the Daily Processing report counts work).
_WORK_EVENTS = (
    "UPLOAD_DOCUMENT", "REUPLOAD_DOCUMENT", "REVIEW_DOCUMENT", "CORRECT_EXTRACTED_FIELD", "ADD_EVIDENCE",
    "ADD_COMMENT", "PROVIDE_FEEDBACK", "REQUEST_CLARIFICATION", "COMPLETE_ACTION", "APPROVE_CORRECTION",
    "REJECT_CORRECTION", "ACCEPT", "REJECT", "ACCEPT_EXCEPTION", "PROVIDE_VEHICLE_ID", "CONFIRM_BREACH",
    "MARK_FALSE_POSITIVE", "CLOSE_TASK", "COMPLETED", "VERDICT",
)


def _scope_sql(dealer: str, outlet: str) -> str:
    """The caller's active business assignments cover the row's dealer and outlet."""
    return f"""
        EXISTS (
          SELECT 1 FROM auditcore.business_assignments ba
          WHERE ba.tenant_id=:t AND ba.security_actor_id=:actor_id
            AND ba.assignment_status='ACTIVE' AND ba.effective_from <= now()
            AND (ba.effective_to IS NULL OR ba.effective_to >= now())
            AND (ba.dealer_id IS NULL
                 OR (ba.dealer_id={dealer} AND (ba.outlet_id IS NULL OR ba.outlet_id={outlet})))
        )
    """


def _bounds(connection: Connection, *, tenant_id: str, start: date, end: date) -> tuple[str, datetime, datetime]:
    row = connection.execute(
        text(
            """
            SELECT COALESCE(p.timezone_name, :fallback) AS tz,
                   (CAST(:a AS date))::timestamp AT TIME ZONE COALESCE(p.timezone_name, :fallback) AS s,
                   (CAST(:b AS date) + 1)::timestamp AT TIME ZONE COALESCE(p.timezone_name, :fallback) AS e
            FROM (SELECT :t AS tenant_id) x
            LEFT JOIN auditcore.projects p ON p.tenant_id = x.tenant_id
            LIMIT 1
            """
        ),
        {"t": tenant_id, "a": start.isoformat(), "b": end.isoformat(), "fallback": _FALLBACK_TIMEZONE},
    ).mappings().one()
    return str(row["tz"]), row["s"], row["e"]


def _empty_uploads() -> dict[str, Any]:
    return {"files": 0, "booking": 0, "delivery": 0, "journeys": 0, "_journeys": set()}


def _empty_tasks() -> dict[str, int]:
    return {"closed": 0, "journey": 0, "taskManager": 0, "closedLate": 0}


def _empty_gate_pass() -> dict[str, int]:
    return {"days": 0, "uploaded": 0, "finished": 0, "missing": 0}


def _empty_deliveries() -> dict[str, Any]:
    return {"asPerRegister": 0, "updatedInTool": 0, "notNotifiedToPc": 0, "pendingDueToPc": 0,
            "backlogCleared": 0, "_backlog": set()}


def _figures() -> dict[str, Any]:
    return {"uploads": _empty_uploads(), "tasks": _empty_tasks(), "gatePass": _empty_gate_pass(),
            "deliveries": _empty_deliveries()}


def _finish(figures: dict[str, Any]) -> dict[str, Any]:
    """Resolve the distinct-journey sets into counts."""
    uploads = figures.get("uploads")
    if uploads is not None and "_journeys" in uploads:
        uploads["journeys"] = len(uploads.pop("_journeys"))
    deliveries = figures.get("deliveries")
    if deliveries is not None and "_backlog" in deliveries:
        deliveries["backlogCleared"] = len(deliveries.pop("_backlog"))
    return figures


def _is_supervisor(connection: Connection, *, tenant_id: str, actor_id: str, functional_role: str | None,
                   outlets: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    if (functional_role or "").upper() in _ADMIN_ROLES:
        return True, [str(functional_role).upper()]
    roles = sorted({str(r).upper() for o in outlets for r in (o.get("roles") or []) if r})
    if not roles:
        held = connection.execute(
            text(
                """
                SELECT DISTINCT upper(business_role_code) FROM auditcore.business_assignments
                WHERE tenant_id=:t AND security_actor_id=:a AND assignment_status='ACTIVE'
                  AND effective_from <= now() AND (effective_to IS NULL OR effective_to >= now())
                """
            ),
            {"t": tenant_id, "a": actor_id},
        ).scalars().all()
        roles = sorted(str(r) for r in held if r)
    return any(r in _SUPERVISOR_ROLES for r in roles), roles


def _uploads(connection: Connection, *, params: dict[str, Any], only_actor: str | None) -> list[dict[str, Any]]:
    actor_clause = "AND ub.uploaded_by_actor_id = :self" if only_actor else ""
    scope = _scope_sql("j.dealer_id", "j.outlet_id")
    return [dict(r) for r in connection.execute(
        text(
            f"""
            SELECT ub.batch_id, ub.journey_id, ub.uploaded_by_actor_id AS actor_id,
                   (ub.created_at_utc AT TIME ZONE :tz)::date AS day,
                   j.dealer_id, j.outlet_id,
                   (LEAST(
                      (SELECT MIN(e.created_at_utc) FROM auditcore.p2_activity_events e
                       WHERE e.tenant_id=ub.tenant_id AND e.journey_id=ub.journey_id
                         AND e.event_type='STAGE_CHANGED' AND (e.details->>'to') = ANY(:stages)),
                      ds.first_started_at_utc
                    ) <= ub.created_at_utc) AS delivery
            FROM auditcore.p2_upload_batches ub
            JOIN auditcore.journeys j ON j.tenant_id=ub.tenant_id AND j.journey_id=ub.journey_id
            LEFT JOIN auditcore.journey_stage_states ds
              ON ds.tenant_id=j.tenant_id AND ds.journey_id=j.journey_id AND ds.stage_code='DELIVERY'
            WHERE ub.tenant_id=:t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
              AND ub.batch_status <> 'AWAITING_UPLOAD'
              AND NOT (ub.batch_status='CANCELLED' AND ub.page_count=0)
              {actor_clause}
              AND {scope}
            """
        ),
        {**params, "self": only_actor, "stages": list(_DELIVERY_STAGES)},
    ).mappings().all()]


def _tasks(connection: Connection, *, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every task that closed in the range, in scope, with the person it counts for."""
    scope = _scope_sql("COALESCE(t.dealer_id, j.dealer_id)", "COALESCE(t.outlet_id, j.outlet_id)")
    return [dict(r) for r in connection.execute(
        text(
            f"""
            SELECT t.task_id, t.scope_kind, t.journey_id, t.due_at_utc, t.completion_protocol,
                   t.completed_by_actor_id, t.reviewer_actor_id, t.verified_at_utc,
                   upper(t.assigned_role_code) AS role,
                   COALESCE(t.assigned_actor_id,
                            CASE WHEN upper(t.assigned_role_code) = 'PC' THEN j.created_by_actor_id END) AS person,
                   COALESCE(t.action_completed_at_utc,
                            CASE WHEN t.task_status = 'VERIFIED_COMPLETE' THEN t.verified_at_utc END) AS done_at,
                   COALESCE(t.dealer_id, j.dealer_id) AS dealer_id, COALESCE(t.outlet_id, j.outlet_id) AS outlet_id
            FROM auditcore.p2_tasks t
            LEFT JOIN auditcore.journeys j ON j.tenant_id=t.tenant_id AND j.journey_id=t.journey_id
            WHERE t.tenant_id=:t AND t.task_status NOT IN ('CANCELLED', 'DEAD_LETTER')
              AND (t.scope_kind <> 'JOURNEY' OR upper(t.assigned_role_code) = ANY(:roles))
              AND (
                    (t.action_completed_at_utc >= :s AND t.action_completed_at_utc < :e)
                 OR (t.action_completed_at_utc IS NULL AND t.task_status = 'VERIFIED_COMPLETE'
                     AND t.verified_at_utc >= :s AND t.verified_at_utc < :e)
                 OR (t.verified_at_utc >= :s AND t.verified_at_utc < :e)
              )
              AND {scope}
            """
        ),
        {**params, "roles": list(_TASK_ROLES)},
    ).mappings().all()]


def _journey_tasks_worked(connection: Connection, *, params: dict[str, Any], actor_id: str) -> tuple[int, int]:
    """Journey tasks the caller worked in the range (a task counts once), and how
    many of those closed in the range -- the Daily Processing "work done" reading."""
    scope = _scope_sql("j.dealer_id", "j.outlet_id")
    rows = connection.execute(
        text(
            f"""
            SELECT DISTINCT te.task_id,
                   (tk.task_status = 'VERIFIED_COMPLETE' AND tk.verified_at_utc >= :s AND tk.verified_at_utc < :e)
                     AS closed
            FROM auditcore.p2_task_events te
            JOIN auditcore.p2_tasks tk ON tk.tenant_id=te.tenant_id AND tk.task_id=te.task_id
            JOIN auditcore.journeys j ON j.tenant_id=tk.tenant_id AND j.journey_id=tk.journey_id
            WHERE te.tenant_id=:t AND te.created_at_utc >= :s AND te.created_at_utc < :e
              AND te.actor_id = :self AND te.event_type = ANY(:work) AND tk.scope_kind = 'JOURNEY'
              AND {scope}
            """
        ),
        {**params, "self": actor_id, "work": list(_WORK_EVENTS)},
    ).mappings().all()
    return len(rows), sum(1 for r in rows if r["closed"])


def _backlog(connection: Connection, *, params: dict[str, Any], only_actor: str | None) -> list[dict[str, Any]]:
    """Vehicle gate passes uploaded in the range: the Journey, the outlet, the
    uploader, the day of the upload and the delivery date read off the gate pass."""
    actor_clause = "AND u.actor_id = :self" if only_actor else ""
    scope = _scope_sql("j.dealer_id", "j.outlet_id")
    return [dict(r) for r in connection.execute(
        text(
            f"""
            WITH dated AS (
                SELECT DISTINCT ON (f.journey_id, f.di_document_id)
                       f.journey_id, f.di_document_id, f.effective_value #>> '{{}}' AS v
                FROM auditcore.journey_document_extracted_fields f
                JOIN auditcore.evidence e
                  ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
                 AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
                WHERE f.tenant_id = :t AND f.field_key = 'delivery_date'
                  AND COALESCE(e.document_type_key, f.source_document_type_key) = 'gate_pass'
                ORDER BY f.journey_id, f.di_document_id, f.source_fact_version DESC NULLS LAST,
                         f.updated_at_utc DESC
            ),
            uploads AS (
                SELECT d.journey_id, d.v, ub.uploaded_by_actor_id AS actor_id, MIN(ub.created_at_utc) AS uploaded_at
                FROM dated d
                JOIN auditcore.p2_document_queue q ON q.tenant_id = :t AND q.di_document_id = d.di_document_id
                JOIN auditcore.p2_upload_batches ub ON ub.tenant_id = q.tenant_id AND ub.batch_id = q.batch_id
                WHERE d.v ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}$'
                GROUP BY d.journey_id, d.v, ub.uploaded_by_actor_id
            )
            SELECT u.journey_id, u.actor_id, j.dealer_id, j.outlet_id,
                   (u.uploaded_at AT TIME ZONE :tz)::date AS day, CAST(u.v AS date) AS gate_pass_date
            FROM uploads u
            JOIN auditcore.journeys j ON j.tenant_id = :t AND j.journey_id = u.journey_id
            WHERE u.uploaded_at >= :s AND u.uploaded_at < :e
              {actor_clause}
              AND {scope}
            """
        ),
        {**params, "self": only_actor},
    ).mappings().all()]


def _gate_pass_day(value: date) -> date:
    """Sunday's deliveries belong to Monday's Daily Gate Pass."""
    return value + timedelta(days=1) if value.isoweekday() == 7 else value


def build_day_stats(connection: Connection, *, tenant_id: str, actor_id: str, functional_role: str | None,
                    start: date, end: date) -> dict[str, Any]:
    timezone_name, s, e = _bounds(connection, tenant_id=tenant_id, start=start, end=end)
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(_FALLBACK_TIMEZONE)
    params = {"t": tenant_id, "actor_id": actor_id, "s": s, "e": e, "tz": timezone_name}
    outlets = scope_outlets(connection, tenant_id, actor_id)
    supervisor, roles = _is_supervisor(connection, tenant_id=tenant_id, actor_id=actor_id,
                                       functional_role=functional_role, outlets=outlets)
    names = known_names(connection, tenant_id=tenant_id)
    only_actor = None if supervisor else actor_id

    # ---- the tree: dealer -> outlet -> pc, plus a flat pc list and a day series
    dealers: dict[str, dict[str, Any]] = {}
    outlet_rows: dict[str, dict[str, Any]] = {}
    pcs: dict[str, dict[str, Any]] = {}
    days: dict[str, dict[str, Any]] = {}
    totals = _figures()
    for i in range((end - start).days + 1):
        key = (start + timedelta(days=i)).isoformat()
        days[key] = {"date": key, "uploads": _empty_uploads(), "tasks": _empty_tasks(),
                     "deliveries": {"updatedInTool": 0, "asPerRegister": 0, "backlogCleared": 0, "_backlog": set()}}
    for o in outlets:
        did, oid = str(o["dealer_id"]), str(o["outlet_id"])
        dealer = dealers.setdefault(did, {"dealerId": did, "dealerName": o["dealer_name"], **_figures(), "outlets": []})
        row = {"outletId": oid, "outletName": o["outlet_name"], "dealerId": did, "dealerName": o["dealer_name"],
               "classification": o["outlet_classification"],
               "gatePassApplicable": o["outlet_classification"] == "ONSITE",
               **_figures(), "pcs": {}, "gatePassDays": []}
        outlet_rows[oid] = row
        dealer["outlets"].append(row)

    def pc_row(container: dict[str, Any], actor: str | None) -> dict[str, Any]:
        key = actor or ""
        return container.setdefault(key, {
            "actorId": actor, "name": names.get(key), "uploads": _empty_uploads(), "tasks": _empty_tasks(),
            "gatePass": {"uploaded": 0}, "_outlets": set(),
        })

    def add_upload(target: dict[str, Any], journey_id: Any, delivery: bool) -> None:
        up = target["uploads"]
        up["files"] += 1
        up["delivery" if delivery else "booking"] += 1
        up["_journeys"].add(str(journey_id))

    for u in _uploads(connection, params=params, only_actor=only_actor):
        oid, did = str(u["outlet_id"]), str(u["dealer_id"])
        delivery = bool(u["delivery"])
        add_upload(totals, u["journey_id"], delivery)
        day = days.get(u["day"].isoformat())
        if day is not None:
            add_upload(day, u["journey_id"], delivery)
        outlet = outlet_rows.get(oid)
        if outlet is not None:
            add_upload(outlet, u["journey_id"], delivery)
            add_upload(dealers[did], u["journey_id"], delivery)
            add_upload(pc_row(outlet["pcs"], u["actor_id"]), u["journey_id"], delivery)
        flat = pc_row(pcs, u["actor_id"])
        add_upload(flat, u["journey_id"], delivery)
        if outlet is not None:
            flat["_outlets"].add(outlet["outletName"])

    def add_task(target: dict[str, Any], task: dict[str, Any]) -> None:
        tk = target["tasks"]
        tk["closed"] += 1
        tk["journey" if task["scope_kind"] == "JOURNEY" else "taskManager"] += 1
        if task["due_at_utc"] is not None and task["done_at"] is not None and task["done_at"] > task["due_at_utc"]:
            tk["closedLate"] += 1

    me = {"uploads": _empty_uploads(), "tasks": _empty_tasks(), "tasksReviewed": 0}
    for t in _tasks(connection, params=params):
        done = t["done_at"]
        closed_in_range = done is not None and s <= done < e
        person = str(t["person"]) if t["person"] else None
        if (t["completion_protocol"] == "REQUESTER_CONFIRMED" and t["verified_at_utc"] is not None
                and s <= t["verified_at_utc"] < e and t["reviewer_actor_id"] == actor_id
                and t["completed_by_actor_id"] != actor_id):
            me["tasksReviewed"] += 1
        if not closed_in_range:
            continue
        if person == actor_id or t["completed_by_actor_id"] == actor_id:
            add_task(me, t)
        if only_actor and person != only_actor and t["completed_by_actor_id"] != only_actor:
            continue
        add_task(totals, t)
        day = days.get(done.astimezone(zone).date().isoformat())
        if day is not None:
            add_task(day, t)
        oid, did = (str(t["outlet_id"]) if t["outlet_id"] else None), (str(t["dealer_id"]) if t["dealer_id"] else None)
        outlet = outlet_rows.get(oid or "")
        if outlet is not None:
            add_task(outlet, t)
            if did in dealers:
                add_task(dealers[did], t)
            add_task(pc_row(outlet["pcs"], person), t)
        flat = pc_row(pcs, person)
        add_task(flat, t)
        if outlet is not None:
            flat["_outlets"].add(outlet["outletName"])
    worked, worked_closed = _journey_tasks_worked(connection, params=params, actor_id=actor_id)
    me["journeyTasksWorked"] = worked
    me["journeyTasksWorkedClosed"] = worked_closed
    me["uploads"] = pcs.get(actor_id, {}).get("uploads") or _empty_uploads()

    # ---- the gate pass block: the consolidated view's own figures
    gp = build_gate_pass_summary(connection, tenant_id=tenant_id, actor_id=actor_id, start=start, end=end)
    for o in gp["outlets"]:
        outlet = outlet_rows.get(o["outletId"])
        if outlet is None:
            continue
        g = {"days": o["gatePassDays"],
             "uploaded": sum(1 for d in o["days"] if d["status"] != "NOT_UPLOADED"),
             "finished": o["gatePassesFinished"], "missing": o["gatePassesMissing"]}
        d = {"asPerRegister": o["asPerRegister"], "updatedInTool": o["updatedInTool"],
             "notNotifiedToPc": o["notNotifiedToPc"], "pendingDueToPc": o["pendingDueToPc"]}
        for target in (outlet, dealers[outlet["dealerId"]], totals):
            for k, v in g.items():
                target["gatePass"][k] += v
            for k, v in d.items():
                target["deliveries"][k] += v
        outlet["gatePassDays"] = o["days"]
        for entry in o["days"]:
            day = days.get(entry["date"])
            if day is not None:
                day["deliveries"]["updatedInTool"] += entry["updatedInTool"]
                day["deliveries"]["asPerRegister"] += entry["asPerRegister"] or 0
    uploaded_by = connection.execute(
        text(
            """
            SELECT pc_actor_id, count(*) AS n FROM auditcore.p2_gate_passes
            WHERE tenant_id=:t AND status='ACTIVE' AND business_date BETWEEN :a AND :b
              AND outlet_id = ANY(CAST(:ids AS uuid[]))
            GROUP BY pc_actor_id
            """
        ),
        {"t": tenant_id, "a": start, "b": end, "ids": list(outlet_rows)},
    ).mappings().all()
    for r in uploaded_by:
        if only_actor and str(r["pc_actor_id"]) != only_actor:
            continue
        pc_row(pcs, str(r["pc_actor_id"]))["gatePass"]["uploaded"] += int(r["n"])
    me["gatePass"] = {"uploaded": pcs.get(actor_id, {}).get("gatePass", {}).get("uploaded", 0)}

    for b in _backlog(connection, params=params, only_actor=only_actor):
        if _gate_pass_day(b["gate_pass_date"]) >= b["day"]:
            continue
        jid = str(b["journey_id"])
        totals["deliveries"]["_backlog"].add(jid)
        day = days.get(b["day"].isoformat())
        if day is not None:
            day["deliveries"]["_backlog"].add(jid)
        outlet = outlet_rows.get(str(b["outlet_id"]))
        if outlet is not None:
            outlet["deliveries"]["_backlog"].add(jid)
            dealers[outlet["dealerId"]]["deliveries"]["_backlog"].add(jid)

    # ---- shape the answer
    def finish_pc(row: dict[str, Any]) -> dict[str, Any]:
        row = _finish(row)
        row["outlets"] = sorted(row.pop("_outlets", set()))
        return row

    flat_pcs = sorted((finish_pc(p) for p in pcs.values()),
                      key=lambda p: (-(p["uploads"]["files"] + p["tasks"]["closed"]), p["name"] or p["actorId"] or ""))
    dealer_list = []
    for d in dealers.values():
        for o in d["outlets"]:
            o["pcs"] = sorted((finish_pc(p) for p in o["pcs"].values()),
                              key=lambda p: (-(p["uploads"]["files"] + p["tasks"]["closed"]), p["name"] or ""))
            _finish(o)
        d["outlets"].sort(key=lambda o: (o["classification"] != "ONSITE", o["outletName"].lower()))
        dealer_list.append(_finish(d))
    dealer_list.sort(key=lambda d: d["dealerName"].lower())
    day_list = []
    for d in days.values():
        d["uploads"]["journeys"] = len(d["uploads"].pop("_journeys"))
        d["deliveries"]["backlogCleared"] = len(d["deliveries"].pop("_backlog"))
        day_list.append(d)
    _finish(totals)
    me["uploads"] = dict(me["uploads"])
    if "_journeys" in me["uploads"]:
        me["uploads"]["journeys"] = len(me["uploads"].pop("_journeys"))

    return {
        "from": start.isoformat(), "to": end.isoformat(), "timezone": timezone_name,
        "caller": {"actorId": actor_id, "name": names.get(actor_id), "supervisor": supervisor, "roles": roles},
        "totals": totals,
        "dealers": dealer_list if supervisor else [],
        "outlets": [o for d in dealer_list for o in d["outlets"]],
        "pcs": flat_pcs if supervisor else [p for p in flat_pcs if p["actorId"] == actor_id],
        "days": day_list,
        "self": me,
        "notes": [
            ("A file is a Delivery upload when the Journey was already in delivery (Booking complete or a "
             "Delivery stage) when it was uploaded; otherwise a Booking upload. A cancelled duplicate is not "
             "an upload."),
            ("Tasks closed: Journey Tasks and Task Manager tasks whose action was completed in the period, "
             "counted for the assignee (the Journey's PC for a PC task); closed late is against the due time."),
            ("Daily gate pass and the deliveries figures are those of the consolidated gate pass view: onsite "
             "outlets only, Monday's gate pass covers Sunday, and a delivery counts on the day its vehicle gate "
             "pass is dated. Backlog cleared: deliveries whose vehicle gate pass, dated an earlier day, was "
             "uploaded in the period -- counted apart from Updated in tool."),
        ],
    }


@router.get("/reports/day-stats")
def get_day_stats(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    """The day-stats card of the Employee (PC) and Team Lead dashboards: ``from``..``to``
    (days in the Project's timezone, today when left out, at most 31 days)."""
    access = authorize_p2(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    today = ist_day().date()
    end = min(end or today, today)
    start = start or end
    if start > end or (end - start).days >= MAX_SUMMARY_DAYS:
        raise HTTPException(status_code=422, detail=f"Choose a range of 1 to {MAX_SUMMARY_DAYS} days.")
    return build_day_stats(
        connection, tenant_id=tenant_id, actor_id=human_principal.subject,
        functional_role=access.functional_role, start=start, end=end,
    )
