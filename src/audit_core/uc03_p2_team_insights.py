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


def backlog(connection: Connection, *, tenant_id: str, zone: Any, now: datetime) -> dict[str, Any]:
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

    delivery, closed_ever = _delivery_state(connection, tenant_id, now)
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


def efficiency(connection: Connection, *, tenant_id: str, start: datetime, end: datetime, now: datetime) -> dict[str, Any]:
    """Per PC over the window: delivery files started and closed, days to close, tasks in time."""
    delivery, closed_ever = _delivery_state(connection, tenant_id, now)
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
    for r in connection.execute(text("""
        SELECT t.completed_by_actor_id AS person, count(*) AS n,
               count(*) FILTER (WHERE t.action_completed_at_utc <= t.due_at_utc) AS in_time
        FROM auditcore.p2_tasks t
        WHERE t.tenant_id = :t AND t.action_completed_at_utc >= :s AND t.action_completed_at_utc < :e
          AND t.due_at_utc IS NOT NULL AND t.completed_by_actor_id IS NOT NULL
        GROUP BY 1
    """), {"t": tenant_id, "s": start, "e": end}).mappings():
        cell = eff[str(r["person"])]
        cell["tasksWithDue"] += int(r["n"])
        cell["tasksInTime"] += int(r["in_time"])

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

    for r in connection.execute(text("""
        SELECT j.outlet_id AS outlet, x.day, count(*) FILTER (WHERE x.delivery) AS deliveries,
               count(*) FILTER (WHERE NOT x.delivery) AS bookings
        FROM (
          SELECT ub.journey_id, (ub.created_at_utc AT TIME ZONE :tz)::date AS day,
                 COALESCE(bool_or(q.business_stage = 'DELIVERY'), false) AS delivery
          FROM auditcore.p2_upload_batches ub
          LEFT JOIN auditcore.p2_document_queue q ON q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
          WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
            AND ub.batch_status <> 'AWAITING_UPLOAD'
            AND NOT (ub.batch_status = 'CANCELLED' AND COALESCE(ub.page_count, 0) = 0)
          GROUP BY 1, 2
        ) x
        JOIN auditcore.journeys j ON j.tenant_id = :t AND j.journey_id = x.journey_id
        GROUP BY 1, 2
    """), {"t": tenant_id, "s": start, "e": end, "tz": tz}).mappings():
        c = cell_of(r["outlet"], r["day"].isoformat())
        c["bookings"] += int(r["bookings"])
        c["deliveries"] += int(r["deliveries"])
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
    return flow


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



# The lines of a person's daily report that come from what they did on tasks.
_REPORT_EVENTS = {"CONFIRM_BREACH": "breach", "MARK_FALSE_POSITIVE": "noBreach", "REJECT": "sentBack",
                  "REJECT_CORRECTION": "sentBack", "REQUEST_CLARIFICATION": "sentBack"}


def report(connection: Connection, *, tenant_id: str, start: datetime, end: datetime, tz: str) -> dict[str, Any]:
    """Per person and day, for the daily report (product owner 13 Nov 2026, from a Team Lead's own
    daily reporting): booking and delivery files updated, breach / no breach / sent back, payments
    verified, and the Task Manager tasks they completed (their titles: the "other" work)."""
    counts: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    other: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    def put(person: Any, day: Any, key: str, n: int) -> None:
        if person and n:
            cell = counts[str(person)][key]
            cell[day.isoformat()] = cell.get(day.isoformat(), 0) + int(n)

    params = {"t": tenant_id, "s": start, "e": end, "tz": tz}
    for r in connection.execute(text("""
        SELECT x.person, x.day, count(*) FILTER (WHERE x.delivery) AS deliveries,
               count(*) FILTER (WHERE NOT x.delivery) AS bookings
        FROM (
          SELECT ub.uploaded_by_actor_id AS person, ub.journey_id, (ub.created_at_utc AT TIME ZONE :tz)::date AS day,
                 COALESCE(bool_or(q.business_stage = 'DELIVERY'), false) AS delivery
          FROM auditcore.p2_upload_batches ub
          LEFT JOIN auditcore.p2_document_queue q ON q.tenant_id = ub.tenant_id AND q.batch_id = ub.batch_id
          WHERE ub.tenant_id = :t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
            AND ub.batch_status <> 'AWAITING_UPLOAD'
            AND NOT (ub.batch_status = 'CANCELLED' AND COALESCE(ub.page_count, 0) = 0)
          GROUP BY 1, 2, 3
        ) x GROUP BY 1, 2
    """), params).mappings():
        put(r["person"], r["day"], "bookingFiles", r["bookings"])
        put(r["person"], r["day"], "deliveryFiles", r["deliveries"])
    for r in connection.execute(text("""
        SELECT actor_id AS person, (created_at_utc AT TIME ZONE :tz)::date AS day, event_type, count(*) AS n
        FROM auditcore.p2_task_events
        WHERE tenant_id = :t AND created_at_utc >= :s AND created_at_utc < :e AND actor_id IS NOT NULL
          AND event_type = ANY(:events)
        GROUP BY 1, 2, 3
    """), {**params, "events": sorted(_REPORT_EVENTS)}).mappings():
        put(r["person"], r["day"], _REPORT_EVENTS[r["event_type"]], r["n"])
    for r in connection.execute(text("""
        SELECT verified_by_actor_id AS person, (occurred_at_utc AT TIME ZONE :tz)::date AS day, count(*) AS n
        FROM auditcore.payment_verification_events
        WHERE tenant_id = :t AND occurred_at_utc >= :s AND occurred_at_utc < :e
          AND upper(COALESCE(verified_by_role_code, '')) <> 'SYSTEM'
        GROUP BY 1, 2
    """), params).mappings():
        put(r["person"], r["day"], "paymentsVerified", r["n"])
    for r in connection.execute(text("""
        SELECT t.completed_by_actor_id AS person, (t.action_completed_at_utc AT TIME ZONE :tz)::date AS day, t.title
        FROM auditcore.p2_tasks t
        WHERE t.tenant_id = :t AND t.scope_kind <> 'JOURNEY' AND t.completed_by_actor_id IS NOT NULL
          AND t.action_completed_at_utc >= :s AND t.action_completed_at_utc < :e
        ORDER BY t.action_completed_at_utc
        LIMIT 2000
    """), params).mappings():
        other[str(r["person"])][r["day"].isoformat()].append(str(r["title"]))
    return {"counts": counts, "other": other}



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
                 (ub.created_at_utc AT TIME ZONE :tz)::date AS day,
                 (SELECT CASE WHEN count(*) = 0 THEN NULL WHEN bool_or(bu.source = 'DRIVE') THEN 'drive' ELSE 'bulk' END
                  FROM auditcore.p2_bulk_files bf
                  JOIN auditcore.p2_bulk_uploads bu ON bu.tenant_id = bf.tenant_id AND bu.bulk_id = bf.bulk_id
                  WHERE bf.tenant_id = ub.tenant_id AND bf.batch_id = ub.batch_id) AS channel
          FROM auditcore.p2_upload_batches ub
          JOIN auditcore.journeys j ON j.tenant_id = ub.tenant_id AND j.journey_id = ub.journey_id
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
