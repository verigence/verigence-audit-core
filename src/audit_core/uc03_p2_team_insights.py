"""What a lead manages by (product owner, 13 Nov 2026), read once per Project and window and kept
with the Performance figures:

"Do I have visibility how much work is pending, the efficiency of PC, how PC/Reviewers/TL have
been performing day to day, week to week, how many delinquencies in daily/weekly tasks for each
PC, how many deliveries per outlet/dealer on daily/weekly, booking/delivery."

pending     as of now, by age (today, 1-3 days, 4-7 days, older), per outlet and per person:
            deliveryFilesOpen   delivery files uploaded and not yet closed (the PC's rule), on the
                                Journey's PC; of which missingDocuments still lack a mandatory one
            tasksOpen           open tasks of PCs; of which tasksOverdue are past their due time
            pagesToReupload     pages that failed for good in the last 30 days and were not sent again
            awaitingReview      Booking / Delivery stages with documents and no reviewed mark (outlet)
            awaitingDecision    Journeys a Reviewer closed that the TL / PM has not decided
efficiency  per PC over the window: delivery files started and closed, the days from the first
            delivery upload to closing, tasks closed with a due time and of those in time
flow        per outlet and day: bookings and deliveries uploaded (a Journey uploaded to that day
            counts as a delivery when a delivery page is among that day's uploads, else a booking),
            and the gate register: deliveries on the register, in the tool, not notified to the PC
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text

from audit_core.uc03_p2_project_tracker import _CLOSING_EVENTS as _TASK_CLOSING

PENDING_KINDS: tuple[dict[str, str], ...] = (
    {"key": "deliveriesNotUploaded", "label": "Deliveries not uploaded"},
    {"key": "deliveryFilesOpen", "label": "Delivery files not closed"},
    {"key": "missingDocuments", "label": "Held by missing documents"},
    {"key": "tasksOpen", "label": "Open tasks (PC)"},
    {"key": "tasksOverdue", "label": "Overdue tasks"},
    {"key": "paymentsNotReconciled", "label": "Payments not reconciled"},
    {"key": "pagesToReupload", "label": "Pages to upload again"},
    {"key": "awaitingReview", "label": "Awaiting Reviewer"},
    {"key": "awaitingDecision", "label": "Awaiting TL / PM"},
)
BUCKETS = ("Today", "1-3 days", "4-7 days", "Older")
_REGISTER_BACK_DAYS = 30  # deliveries on a gate register this far back still count as "not uploaded"
_LOOK_BACK_DAYS = 120  # work older than this is not "pending": it is stale and belongs to a clean-up
_OPEN_TASK_END = ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")


def bucket(age_days: int) -> int:
    return 0 if age_days <= 0 else 1 if age_days <= 3 else 2 if age_days <= 7 else 3


def _blank() -> dict[str, list[int]]:
    return {k["key"]: [0, 0, 0, 0] for k in PENDING_KINDS}


def _delivery_state(connection: Connection, tenant_id: str, now: datetime) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Every Journey with a delivery upload (its first one, outlet, PC, documents still missing) and
    the delivery files closed so far (the PC's rule)."""
    from audit_core.uc03_p2_daily_report import delivery_files_closed

    delivery = {str(r["journey_id"]): dict(r) for r in connection.execute(text("""
        SELECT q.journey_id, MIN(q.created_at_utc) AS first_upload, j.outlet_id, j.created_by_actor_id AS pc,
               COALESCE(jsonb_array_length(g.details->'missing'), 0) AS missing
        FROM auditcore.p2_document_queue q
        JOIN auditcore.journeys j ON j.tenant_id = q.tenant_id AND j.journey_id = q.journey_id
        LEFT JOIN auditcore.p2_stage_gate_state g ON g.tenant_id = j.tenant_id AND g.journey_id = j.journey_id
             AND g.stage_code = 'DELIVERY' AND g.gate_key = 'REQUIRED_DOCUMENTS'
        WHERE q.tenant_id = :t AND q.business_stage = 'DELIVERY'
        GROUP BY q.journey_id, j.outlet_id, j.created_by_actor_id, g.details
    """), {"t": tenant_id}).mappings()}
    closed_ever = delivery_files_closed(connection, tenant_id=tenant_id, actor_id="", names={},
                                        start=datetime(2000, 1, 1, tzinfo=UTC), end=now, whole_project=True)["files"]
    return delivery, closed_ever


def _register_outlets(connection: Connection, tenant_id: str) -> tuple[list[dict[str, Any]], date | None]:
    """The outlets with a PC mapped (those that owe a gate register), and the day the register started."""
    outlets = [dict(r) for r in connection.execute(text("""
        SELECT o.outlet_id, o.outlet_classification FROM auditcore.dealer_outlets o
        WHERE o.tenant_id = :t AND EXISTS (
          SELECT 1 FROM auditcore.business_assignments ba
          WHERE ba.tenant_id = o.tenant_id AND ba.outlet_id = o.outlet_id AND upper(ba.business_role_code) = 'PC'
            AND ba.assignment_status = 'ACTIVE' AND ba.effective_from <= now()
            AND (ba.effective_to IS NULL OR ba.effective_to >= now()))
    """), {"t": tenant_id}).mappings()]
    starts = connection.execute(text(
        "SELECT starts_on FROM auditcore.p2_task_schedules WHERE tenant_id = :t AND system_kind = 'GATE_PASS'"),
        {"t": tenant_id}).scalar_one_or_none()
    return outlets, starts


def backlog(connection: Connection, *, tenant_id: str, zone: Any, now: datetime, state: Any = None) -> dict[str, Any]:
    """What is waiting right now, by age, per outlet and per person."""
    from audit_core.uc03_p2_my_work import gate_register

    today = now.astimezone(zone).date()
    since = now - timedelta(days=_LOOK_BACK_DAYS)
    by_outlet: dict[str, dict[str, list[int]]] = defaultdict(_blank)
    by_person: dict[str, dict[str, list[int]]] = defaultdict(_blank)

    def add(kind: str, outlet: Any, person: Any, at: datetime | date | None, n: int = 1) -> None:
        if at is None:
            b = 3
        else:
            day = at.astimezone(zone).date() if isinstance(at, datetime) else at
            b = bucket((today - day).days)
        if outlet:
            by_outlet[str(outlet)][kind][b] += n
        if person:
            by_person[str(person)][kind][b] += n

    # ---- deliveries on the gate register, intimated to the PC and not uploaded (last 30 days)
    outlets, starts = _register_outlets(connection, tenant_id)
    first = today - timedelta(days=_REGISTER_BACK_DAYS)
    if outlets and starts is not None and max(first, starts) <= today:
        for oid, days in gate_register(connection, tenant_id, outlets, max(first, starts), today, now=now).items():
            for d in days:
                waiting = int(d["pendingWithPc"] or 0)
                if d["gatePassId"] is not None and waiting > 0:
                    add("deliveriesNotUploaded", oid, d["pcActorId"], date.fromisoformat(d["date"]), waiting)

    # ---- payments not reconciled with the bank statement (open PAYMENT_UNVERIFIED findings)
    for r in connection.execute(text("""
        SELECT j.outlet_id AS outlet, j.created_by_actor_id AS person, f.created_at_utc
        FROM auditcore.audit_findings f
        JOIN auditcore.journeys j ON j.tenant_id = f.tenant_id AND j.journey_id = f.journey_id
        WHERE f.tenant_id = :t AND f.finding_type_code = 'PAYMENT_UNVERIFIED' AND f.finding_status IN ('OPEN', 'ACKNOWLEDGED')
          AND f.created_at_utc >= :since
    """), {"t": tenant_id, "since": since}).mappings():
        add("paymentsNotReconciled", r["outlet"], r["person"], r["created_at_utc"])

    delivery, closed_ever = state or _delivery_state(connection, tenant_id, now)
    # ---- delivery files uploaded and not closed
    closed_ids = {f["journeyId"] for f in closed_ever}
    reviewer_closed = {str(r[0]) for r in connection.execute(text(
        "SELECT journey_id FROM auditcore.p2_journey_closures WHERE tenant_id = :t AND acceptance_status <> 'REJECTED'"),
        {"t": tenant_id})}
    for journey_id, d in delivery.items():
        if journey_id in closed_ids or journey_id in reviewer_closed or d["first_upload"] < since:
            continue
        add("deliveryFilesOpen", d["outlet_id"], d["pc"], d["first_upload"])
        if int(d["missing"] or 0) > 0:
            add("missingDocuments", d["outlet_id"], d["pc"], d["first_upload"])

    # ---- open tasks of PCs
    for r in connection.execute(text("""
        SELECT COALESCE(t.outlet_id, j.outlet_id) AS outlet, COALESCE(t.assigned_actor_id, j.created_by_actor_id) AS person,
               t.created_at_utc, t.due_at_utc
        FROM auditcore.p2_tasks t
        LEFT JOIN auditcore.journeys j ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
        WHERE t.tenant_id = :t AND t.task_status <> ALL(:ended) AND upper(COALESCE(t.assigned_role_code, '')) = 'PC'
          AND t.action_completed_at_utc IS NULL AND t.created_at_utc >= :since
    """), {"t": tenant_id, "ended": list(_OPEN_TASK_END), "since": since}).mappings():
        add("tasksOpen", r["outlet"], r["person"], r["created_at_utc"])
        if r["due_at_utc"] is not None and r["due_at_utc"] < now:
            add("tasksOverdue", r["outlet"], r["person"], r["due_at_utc"])

    # ---- pages that failed for good and were not sent again (last 30 days)
    from audit_core.uc03_p2_runtime import swept_pending_sql

    for r in connection.execute(text(f"""
        SELECT j.outlet_id AS outlet, ub.uploaded_by_actor_id AS person, ub.created_at_utc,
               COALESCE(cardinality(q.page_numbers), 1) AS pages
        FROM auditcore.p2_upload_batches ub
        JOIN auditcore.p2_document_queue q ON q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
        JOIN auditcore.journeys j ON j.tenant_id = ub.tenant_id AND j.journey_id = ub.journey_id
        WHERE ub.tenant_id = :t AND ub.created_at_utc >= :month
          AND (q.queue_status IN ('FAILED', 'DEAD_LETTER') OR {swept_pending_sql("q")})
    """), {"t": tenant_id, "month": now - timedelta(days=30)}).mappings():
        for _ in range(int(r["pages"])):
            add("pagesToReupload", r["outlet"], r["person"], r["created_at_utc"])

    # ---- stages awaiting the Reviewer, closures awaiting the TL / PM
    for r in connection.execute(text("""
        WITH stages AS (
          SELECT dc.journey_id, dc.stage_code, MIN(dc.created_at_utc) AS first_document
          FROM auditcore.document_capture_v2_documents dc
          WHERE dc.tenant_id = :t AND dc.stage_code IN ('BOOKING', 'DELIVERY') AND dc.capture_status <> 'SUPERSEDED'
            AND dc.created_at_utc >= :since
          GROUP BY dc.journey_id, dc.stage_code
        )
        SELECT j.outlet_id AS outlet, s.first_document
        FROM stages s
        JOIN auditcore.journeys j ON j.tenant_id = :t AND j.journey_id = s.journey_id
        WHERE NOT EXISTS (SELECT 1 FROM auditcore.p2_stage_document_reviews r
                          WHERE r.tenant_id = :t AND r.journey_id = s.journey_id AND r.stage_code = s.stage_code)
    """), {"t": tenant_id, "since": since}).mappings():
        add("awaitingReview", r["outlet"], None, r["first_document"])
    for r in connection.execute(text("""
        SELECT j.outlet_id AS outlet, c.closed_by_actor_id AS person, c.closed_at_utc
        FROM auditcore.p2_journey_closures c
        JOIN auditcore.journeys j ON j.tenant_id = c.tenant_id AND j.journey_id = c.journey_id
        WHERE c.tenant_id = :t AND c.acceptance_status = 'PENDING'
    """), {"t": tenant_id}).mappings():
        add("awaitingDecision", r["outlet"], r["person"], r["closed_at_utc"])

    return {"byOutlet": by_outlet, "byPerson": by_person}


def efficiency(connection: Connection, *, tenant_id: str, start: datetime, end: datetime, now: datetime,
               state: Any = None) -> dict[str, Any]:
    """Per person over the window: delivery files started and closed (a PC), days to close, and the
    tasks due in the window that were closed by their due time."""
    delivery, closed_ever = state or _delivery_state(connection, tenant_id, now)
    eff: dict[str, dict[str, float]] = defaultdict(lambda: {
        "deliveryStarted": 0, "deliveryClosed": 0, "daysToCloseSum": 0.0, "tasksWithDue": 0, "tasksInTime": 0})
    for d in delivery.values():
        if d["pc"] and start <= d["first_upload"] < end:
            eff[str(d["pc"])]["deliveryStarted"] += 1
    for f in closed_ever:
        at = datetime.fromisoformat(f["closedAtUtc"]) if f["closedAtUtc"] else None
        if not f["pcActorId"] or at is None or not (start <= at < end):
            continue
        cell = eff[str(f["pcActorId"])]
        cell["deliveryClosed"] += 1
        began = delivery.get(f["journeyId"], {}).get("first_upload")
        if began is not None:
            cell["daysToCloseSum"] += max(0.0, (at - began).total_seconds() / 86400)
    # Tasks in time (product owner 2026-11-14: "how you calculated 100% tasks closed on time"). The
    # old count read only tasks with completed_by_actor_id set -- 139 of ~15,000; most tasks close by
    # verification with no closer recorded, and a task still open past its due time was never counted.
    # Now: every task whose due time fell in the window (and has passed), or that was closed in the
    # window before its due time, counts once, for its owner:
    # the assignee; else, for a PC task, the Journey's PC; else whoever closed it; else, still open,
    # the people of that role mapped to its outlet. In time = closed by the due time; closed late or
    # still open is late. A cancelled task is nobody's to do; a duty (a scheduled task) is counted
    # under duties, not here.
    for r in connection.execute(text("""
        WITH task AS (
          SELECT t.task_id, t.due_at_utc, upper(COALESCE(t.assigned_role_code, '')) AS role,
                 t.assigned_actor_id, j.created_by_actor_id AS pc, j.dealer_id,
                 COALESCE(t.outlet_id, j.outlet_id) AS outlet_id,
                 CASE WHEN t.task_status = 'VERIFIED_COMPLETE' OR t.action_completed_at_utc IS NOT NULL
                      THEN COALESCE(t.action_completed_at_utc, t.verified_at_utc, t.updated_at_utc) END AS done_at
          FROM auditcore.p2_tasks t
          LEFT JOIN auditcore.journeys j ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
          WHERE t.tenant_id = :t AND t.due_at_utc IS NOT NULL AND t.schedule_id IS NULL
            AND t.task_status NOT IN ('CANCELLED', 'DEAD_LETTER')
            AND t.due_at_utc >= :s  -- both cases below are due on or after the start
        ), due AS (
          SELECT * FROM task
          WHERE (due_at_utc >= :s AND due_at_utc < LEAST(:e, :now))
             OR (done_at >= :s AND done_at < :e AND done_at <= due_at_utc)
        ), closer AS (
          SELECT DISTINCT ON (te.task_id) te.task_id, te.actor_id
          FROM auditcore.p2_task_events te JOIN due d ON d.task_id = te.task_id
          WHERE te.tenant_id = :t AND te.actor_id IS NOT NULL AND upper(te.actor_id) <> 'SYSTEM'
            AND te.event_type = ANY(:closing)
          ORDER BY te.task_id, te.created_at_utc DESC
        ), owner AS (
          SELECT d.task_id, d.due_at_utc, d.done_at,
                 COALESCE(d.assigned_actor_id::text, CASE WHEN d.role = 'PC' THEN d.pc::text END,
                          CASE WHEN d.done_at IS NOT NULL THEN c.actor_id::text END) AS person,
                 d.role, d.dealer_id, d.outlet_id
          FROM due d LEFT JOIN closer c ON c.task_id = d.task_id
        )
        SELECT x.person, count(*) AS n,
               count(*) FILTER (WHERE x.done_at IS NOT NULL AND x.done_at <= x.due_at_utc) AS in_time,
               count(*) FILTER (WHERE x.done_at IS NULL) AS open_late
        FROM (
          SELECT o.person, o.task_id, o.due_at_utc, o.done_at FROM owner o WHERE o.person IS NOT NULL
          UNION
          SELECT ba.security_actor_id::text, o.task_id, o.due_at_utc, o.done_at
          FROM owner o
          JOIN auditcore.business_assignments ba
            ON ba.tenant_id = :t AND ba.assignment_status = 'ACTIVE'
           AND ba.effective_from <= :now AND (ba.effective_to IS NULL OR ba.effective_to >= :now)
           AND (upper(ba.business_role_code) = o.role
                OR (o.role = 'REVIEWER' AND upper(ba.business_role_code) = 'CRM'))
           AND (ba.dealer_id IS NULL OR (ba.dealer_id = o.dealer_id
                                         AND (ba.outlet_id IS NULL OR ba.outlet_id = o.outlet_id)))
          WHERE o.person IS NULL AND o.role <> ''
        ) x
        GROUP BY 1
    """), {"t": tenant_id, "s": start, "e": end, "now": now, "closing": sorted(_TASK_CLOSING)}).mappings():
        cell = eff[str(r["person"])]
        cell["tasksWithDue"] += int(r["n"])
        cell["tasksInTime"] += int(r["in_time"])
        cell["tasksOpenLate"] = cell.get("tasksOpenLate", 0) + int(r["open_late"])

    return eff


def flow(connection: Connection, *, tenant_id: str, first: date, last: date, start: datetime, end: datetime,
         tz: str, zone: Any, now: datetime) -> dict[str, Any]:
    """Bookings and deliveries uploaded per outlet and day; the gate register of each day."""
    from audit_core.uc03_p2_my_work import gate_register

    today = now.astimezone(zone).date()
    flow: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)

    def cell_of(outlet: Any, day: str) -> dict[str, int]:
        return flow[str(outlet)].setdefault(day, {"bookings": 0, "deliveries": 0, "onRegister": 0, "inTool": 0,
                                                    "notNotified": 0, "notUploaded": 0, "registerMissing": 0})

    by_person: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    for r in connection.execute(text("""
        SELECT j.outlet_id AS outlet, x.person, x.day, count(*) FILTER (WHERE x.delivery) AS deliveries,
               count(*) FILTER (WHERE NOT x.delivery) AS bookings
        FROM (
          SELECT ub.journey_id, ub.uploaded_by_actor_id AS person, (ub.created_at_utc AT TIME ZONE :tz)::date AS day,
                 COALESCE(bool_or(q.business_stage = 'DELIVERY'), false) AS delivery
          FROM auditcore.p2_upload_batches ub
          LEFT JOIN auditcore.p2_document_queue q ON q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
          WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
            AND ub.batch_status <> 'AWAITING_UPLOAD'
            AND NOT (ub.batch_status = 'CANCELLED' AND COALESCE(ub.page_count, 0) = 0)
          GROUP BY 1, 2, 3
        ) x
        JOIN auditcore.journeys j ON j.tenant_id = :t AND j.journey_id = x.journey_id
        GROUP BY 1, 2, 3
    """), {"t": tenant_id, "s": start, "e": end, "tz": tz}).mappings():
        c = cell_of(r["outlet"], r["day"].isoformat())
        c["bookings"] += int(r["bookings"])
        c["deliveries"] += int(r["deliveries"])
        if r["person"]:
            mine = by_person[str(r["person"])].setdefault(r["day"].isoformat(), {"bookings": 0, "deliveries": 0})
            mine["bookings"] += int(r["bookings"])
            mine["deliveries"] += int(r["deliveries"])
    outlets, starts = _register_outlets(connection, tenant_id)
    if outlets and starts is not None and max(first, starts) <= min(last, today):
        for oid, days in gate_register(connection, tenant_id, outlets, max(first, starts), min(last, today),
                                       now=now).items():
            for d in days:
                c = cell_of(oid, d["date"])
                if d["gatePassId"] is None:
                    c["registerMissing"] += 1 if d["status"] == "MISSED" else 0
                    continue
                c["onRegister"] += int(d["total"] or 0)
                c["inTool"] += int(d["uploadedInTool"] or 0)
                c["notNotified"] += int(d["notIntimated"] or 0)
                c["notUploaded"] += max(0, int(d["pendingWithPc"] or 0))
    return {"byOutlet": flow, "byPerson": by_person}


def tasks(connection: Connection, *, tenant_id: str, start: datetime, end: datetime, tz: str) -> dict[str, Any]:
    """New tasks per day: raised in the window, on the person they are for (the assignee, else the
    Journey's PC) and at their outlet."""
    by_person: dict[str, dict[str, int]] = defaultdict(dict)
    by_outlet: dict[str, dict[str, int]] = defaultdict(dict)
    for r in connection.execute(text("""
        SELECT COALESCE(t.assigned_actor_id, j.created_by_actor_id) AS person, COALESCE(t.outlet_id, j.outlet_id) AS outlet,
               (t.created_at_utc AT TIME ZONE :tz)::date AS day, count(*) AS n
        FROM auditcore.p2_tasks t
        LEFT JOIN auditcore.journeys j ON j.tenant_id = t.tenant_id AND j.journey_id = t.journey_id
        WHERE t.tenant_id = :t AND t.created_at_utc >= :s AND t.created_at_utc < :e AND t.task_status <> 'CANCELLED'
        GROUP BY 1, 2, 3
    """), {"t": tenant_id, "s": start, "e": end, "tz": tz}).mappings():
        day = r["day"].isoformat()
        for owner, key in ((by_person, r["person"]), (by_outlet, r["outlet"])):
            if key:
                owner[str(key)][day] = owner[str(key)].get(day, 0) + int(r["n"])
    return {"raisedByPerson": by_person, "raisedByOutlet": by_outlet}




CHANNELS = ("direct", "bulk", "drive")


def channels(connection: Connection, *, tenant_id: str, start: datetime, end: datetime, tz: str) -> dict[str, Any]:
    """How files came in, per person, outlet and day (product owner 13 Nov 2026: "our different
    options to upload, direct, bulk, drive: how they are being used should also be there"): uploaded
    on its own (app or web), in a bulk (folder) upload, or through Google Drive."""
    by_person: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    by_outlet: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    for r in connection.execute(text("""
        SELECT x.person, x.outlet, x.day, COALESCE(x.channel, 'direct') AS channel, count(*) AS n
        FROM (
          SELECT ub.uploaded_by_actor_id AS person, j.outlet_id AS outlet,
                 (ub.created_at_utc AT TIME ZONE :tz)::date AS day, c.channel
          FROM auditcore.p2_upload_batches ub
          JOIN auditcore.journeys j ON j.tenant_id = ub.tenant_id AND j.journey_id = ub.journey_id
          -- The bulk files read once and joined (not once per upload: that grew with the period).
          LEFT JOIN (
            SELECT bf.batch_id, CASE WHEN bool_or(bu.source = 'DRIVE') THEN 'drive' ELSE 'bulk' END AS channel
            FROM auditcore.p2_bulk_files bf
            JOIN auditcore.p2_bulk_uploads bu ON bu.tenant_id = bf.tenant_id AND bu.bulk_id = bf.bulk_id
            WHERE bf.tenant_id = :t AND bf.batch_id IS NOT NULL
            GROUP BY bf.batch_id
          ) c ON c.batch_id = ub.batch_id
          WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
            AND ub.batch_status <> 'AWAITING_UPLOAD'
            AND NOT (ub.batch_status = 'CANCELLED' AND COALESCE(ub.page_count, 0) = 0)
        ) x GROUP BY 1, 2, 3, 4
    """), {"t": tenant_id, "s": start, "e": end, "tz": tz}).mappings():
        day = r["day"].isoformat()
        for owner, key in ((by_person, r["person"]), (by_outlet, r["outlet"])):
            if key:
                cell = owner[str(key)][r["channel"]]
                cell[day] = cell.get(day, 0) + int(r["n"])
    return {"byPerson": by_person, "byOutlet": by_outlet}



# The bulk / Drive channel of an upload batch: the bulk files read once and joined.
_BATCH_CHANNEL_SQL = """
    SELECT bf.batch_id, CASE WHEN bool_or(bu.source = 'DRIVE') THEN 'drive' ELSE 'bulk' END AS channel
    FROM auditcore.p2_bulk_files bf
    JOIN auditcore.p2_bulk_uploads bu ON bu.tenant_id = bf.tenant_id AND bu.bulk_id = bf.bulk_id
    WHERE bf.tenant_id = :t AND bf.batch_id IS NOT NULL
    GROUP BY bf.batch_id
"""


def channel_issues(connection: Connection, *, tenant_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Per person and outlet, for each way files came in (direct, bulk, Drive), the duplicates and the
    files with errors of the window (product owner 14 Nov 2026: "for each PC I don't see the count of
    file upload channel wise and corresponding duplicacy and errors").

    duplicates  files found to be a copy: a bulk / Drive file set aside as an exact duplicate, and a
                journey deleted as the same file uploaded again (by the channel it came in on: a bulk
                or Drive file was read on it, else direct)
    errors      files with a page that failed or was not read in time, or rejected on image quality,
                or whose upload failed; and a bulk / Drive file that failed before it was read

    Neither is part of the channel's files: a copy is not kept, and a file with an error is counted
    once whatever the number of its pages that failed."""
    from audit_core.uc03_p2_runtime import swept_pending_sql
    from audit_core.uc03_p2_same_file import (
        DELETION_KIND,
        DUPLICATE_UPLOADS_COUNTED_FROM,
    )

    def blank() -> dict[str, dict[str, int]]:
        return {"duplicates": dict.fromkeys(CHANNELS, 0), "errors": dict.fromkeys(CHANNELS, 0)}

    by_person: dict[str, dict[str, dict[str, int]]] = defaultdict(blank)
    by_outlet: dict[str, dict[str, dict[str, int]]] = defaultdict(blank)

    def add(person: Any, outlet: Any, key: str, channel: str, n: int) -> None:
        for owner, who in ((by_person, person), (by_outlet, outlet)):
            if who and n:
                owner[str(who)][key][channel if channel in CHANNELS else "direct"] += n

    params = {"t": tenant_id, "s": start, "e": end}
    # Files with errors, by the batch's channel: one read of the window's batches, the queue
    # checked per batch on its (tenant_id, batch_id) rows.
    for r in connection.execute(text(f"""
        SELECT ub.uploaded_by_actor_id AS person, j.outlet_id AS outlet,
               COALESCE(c.channel, 'direct') AS channel, count(*) AS n
        FROM auditcore.p2_upload_batches ub
        JOIN auditcore.journeys j ON j.tenant_id = ub.tenant_id AND j.journey_id = ub.journey_id
        LEFT JOIN ({_BATCH_CHANNEL_SQL}) c ON c.batch_id = ub.batch_id
        WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
          AND ub.batch_status <> 'AWAITING_UPLOAD'
          AND NOT (ub.batch_status = 'CANCELLED' AND COALESCE(ub.page_count, 0) = 0)
          AND (ub.batch_status = 'FAILED' OR EXISTS (
                SELECT 1 FROM auditcore.p2_document_queue q
                WHERE q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
                  AND (q.queue_status IN ('FAILED', 'DEAD_LETTER')
                       OR (q.queue_status = 'CANCELLED' AND q.last_error LIKE 'DI\\_QUALITY\\_%')
                       OR {swept_pending_sql("q")})))
        GROUP BY 1, 2, 3
    """), params).mappings():
        add(r["person"], r["outlet"], "errors", r["channel"], int(r["n"]))
    # Bulk / Drive files set aside as exact duplicates, or failed before a batch was made.
    for r in connection.execute(text("""
        SELECT bu.uploaded_by_actor_id AS person, bu.outlet_id AS outlet,
               CASE WHEN bu.source = 'DRIVE' THEN 'drive' ELSE 'bulk' END AS channel,
               count(*) FILTER (WHERE bf.outcome = 'EXACT_DUPLICATE') AS duplicates,
               count(*) FILTER (WHERE bf.outcome = 'FAILED' AND bf.batch_id IS NULL) AS errors
        FROM auditcore.p2_bulk_files bf
        JOIN auditcore.p2_bulk_uploads bu ON bu.tenant_id = bf.tenant_id AND bu.bulk_id = bf.bulk_id
        WHERE bf.tenant_id = :t AND bf.created_at_utc >= :s AND bf.created_at_utc < :e
          AND bf.outcome IN ('EXACT_DUPLICATE', 'FAILED')
        GROUP BY 1, 2, 3
    """), params).mappings():
        add(r["person"], r["outlet"], "duplicates", r["channel"], int(r["duplicates"]))
        add(r["person"], r["outlet"], "errors", r["channel"], int(r["errors"]))
    # Journeys deleted as the same file uploaded again, by the channel they came in on.
    for r in connection.execute(text("""
        SELECT d.pc_actor_id AS person, d.outlet_id AS outlet,
               COALESCE((SELECT CASE WHEN bool_or(bu.source = 'DRIVE') THEN 'drive' ELSE 'bulk' END
                         FROM auditcore.p2_bulk_files bf
                         JOIN auditcore.p2_bulk_uploads bu ON bu.tenant_id = bf.tenant_id AND bu.bulk_id = bf.bulk_id
                         WHERE bf.tenant_id = d.tenant_id AND bf.journey_id = d.journey_id
                         HAVING count(*) > 0), 'direct') AS channel,
               count(*) AS n
        FROM auditcore.p2_journey_deletions d
        WHERE d.tenant_id = :t AND d.deletion_kind = :kind AND d.deletion_status = 'COMPLETED'
          AND d.journey_created_at_utc >= GREATEST(CAST(:s AS timestamptz), CAST(:from_ AS timestamptz))
          AND d.journey_created_at_utc < :e
        GROUP BY 1, 2, 3
    """), {**params, "kind": DELETION_KIND, "from_": DUPLICATE_UPLOADS_COUNTED_FROM}).mappings():
        add(r["person"], r["outlet"], "duplicates", r["channel"], int(r["n"]))
    return {"byPerson": by_person, "byOutlet": by_outlet}


QUALITY_KEYS = ("blurred", "faded", "tooSmall", "otherQuality", "duplicatePages", "duplicateFiles")


def quality(connection: Connection, *, tenant_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Duplicates and documents with quality issues, per person and outlet, of the files uploaded in
    the window (product owner 13 Nov 2026: "duplicate files, documents with quality issues should
    be there"): pages rejected on image quality by reason, pages already uploaded and not sent
    again, and uploads deleted as the same file uploaded again."""
    from audit_core.uc03_p2_daily_report import rejection_reason
    from audit_core.uc03_p2_same_file import duplicate_uploads_deleted

    by_person: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(QUALITY_KEYS, 0))
    by_outlet: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(QUALITY_KEYS, 0))

    def add(person: Any, outlet: Any, key: str, n: int) -> None:
        for owner, who in ((by_person, person), (by_outlet, outlet)):
            if who:
                owner[str(who)][key] += n

    names = {"blurred": "blurred", "faded": "faded", "tooSmall": "tooSmall"}
    for r in connection.execute(text("""
        SELECT ub.uploaded_by_actor_id AS person, j.outlet_id AS outlet, q.last_error,
               sum(COALESCE(cardinality(q.page_numbers), 1)) AS n
        FROM auditcore.p2_upload_batches ub
        JOIN auditcore.journeys j ON j.tenant_id = ub.tenant_id AND j.journey_id = ub.journey_id
        JOIN auditcore.p2_document_queue q ON q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
        WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
          AND ((q.last_error LIKE 'DI\\_QUALITY\\_%' AND q.queue_status IN ('FAILED', 'DEAD_LETTER', 'CANCELLED'))
               OR q.last_error = 'DUPLICATE_PAGE')
        GROUP BY 1, 2, 3
    """), {"t": tenant_id, "s": start, "e": end}).mappings():
        if r["last_error"] == "DUPLICATE_PAGE":
            add(r["person"], r["outlet"], "duplicatePages", int(r["n"]))
        else:
            add(r["person"], r["outlet"], names.get(rejection_reason(r["last_error"]) or "", "otherQuality"), int(r["n"]))
    for r in duplicate_uploads_deleted(connection, tenant_id=tenant_id, start=start, end=end):
        add(r["pc_actor_id"], r["outlet_id"], "duplicateFiles", 1)
    return {"byPerson": by_person, "byOutlet": by_outlet}
