"""Daily processing report: how the system behaved for each PC's work, one day.

Asked for 2026-10-20 (TL / PM / Executive tab): per PC, per day, how many
journeys they uploaded, pages per journey, how many documents were read
correctly and how many are still waiting, when the upload started and when
the system finished, the tasks opened per journey, how many automatic
re-attempts were needed, how many times a PC asked for a document to be read
again / the journey re-synced, and what the night jobs corrected.

Every figure is read from what Audit Core stores -- nothing is estimated:

- uploads: ``p2_upload_batches`` (who uploaded, when, pages after the split);
- documents: ``p2_document_queue`` units of those uploads, current state
  (merged fragments and cancelled duplicates are not documents);
- finish time: the last status change of those units to a settled status,
  from the per-page status history ``p2_activity_events``
  DOCUMENT_STATUS_CHANGED (migration 0117). Only given once no unit of the
  journey's uploads that day is still in flight;
- re-attempts and requests: ``p2_activity_events`` -- a page entering
  RETRY_WAIT (automatic retry), P2_WORK_DEAD_LETTER (retries exhausted),
  PAGE_RETRY_REQUESTED / PAGE_REREAD_REQUESTED (by a person, or by the night
  sweep: ``by`` = 'nightly sweep' / 'SYSTEM'), RECHECK_REQUESTED (the
  journey's "Re-sync with Document Intelligence");
- night corrections: night-sweep requests on the day, and how many of those
  pages were read (DOCUMENT_READY) afterwards;
- tasks: ``p2_tasks`` created on the day, plus REOPENED rounds;
- files (2026-10-26): every file uploaded on the day with its state (read,
  still being read, partly failed, never uploaded, could not be split,
  duplicate), a link that opens the original, and a flag when it has more
  pages than LARGE_UPLOAD_PAGES (25) -- with the PC's notice on it and what
  they answered;
- document status (2026-10-26): per PC and for the day, uploaded / not
  uploaded / rejected / retried / read / still waiting, from the above;
- work done (2026-10-26): the human task actions of the day from
  ``p2_task_events`` (a system event is never work): per person, the tasks
  they worked, what they did and which of those tasks closed that day;
- failed pages (2026-10-21, also its own endpoint /reports/failed-pages):
  every page now FAILED, and every file that failed before it was split,
  with the reason, the attempts, when it was uploaded and failed (the
  page's last DOCUMENT_STATUS_CHANGED to FAILED) and a link that opens it.

Day boundaries are the Project's own timezone. Known gaps, stated in the
response rather than guessed: the actor of a journey Re-sync is not stored,
and DI's own nightly reprocessing keeps its per-document results in DI.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
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
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_runtime import LARGE_UPLOAD_PAGES, swept_pending_sql

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-daily-report"])

_READ_PERMISSION = "audit.journey.read"
_REPORT_ROLES = ("TL", "PM", "EXECUTIVE")
_ADMIN_ROLES = ("SUPER_ADMIN", "TENANT_ADMIN")
_FALLBACK_TIMEZONE = "Asia/Kolkata"

_WAITING = frozenset({
    "QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING",
    "CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE", "RETRY_WAIT",
})
_SETTLED = ("READY", "SUPPORTING", "NEEDS_REVIEW", "FAILED", "DEAD_LETTER")
_REJECTED_ERRORS = frozenset({"FILE_EMPTY", "INVALID_FILE_CONTENT", "CORRUPT", "UPLOAD_FAILED"})

# What a person does on a task (the action catalog plus the two events that
# close one). Everything else in p2_task_events -- raised, updated, machine
# verification, returned -- is the system's, not work.
_WORK_EVENTS = (
    "UPLOAD_DOCUMENT", "REUPLOAD_DOCUMENT", "REVIEW_DOCUMENT", "CORRECT_EXTRACTED_FIELD", "ADD_EVIDENCE",
    "ADD_COMMENT", "PROVIDE_FEEDBACK", "REQUEST_CLARIFICATION", "COMPLETE_ACTION", "APPROVE_CORRECTION",
    "REJECT_CORRECTION", "ACCEPT", "REJECT", "ACCEPT_EXCEPTION", "PROVIDE_VEHICLE_ID", "CONFIRM_BREACH",
    "MARK_FALSE_POSITIVE", "CLOSE_TASK", "COMPLETED", "VERDICT",
)
_WORK_TASKS_PER_PERSON = 100
_FILE_ROWS_LIMIT = 2000

_SCOPE = """
    EXISTS (
      SELECT 1 FROM auditcore.business_assignments ba
      WHERE ba.tenant_id=j.tenant_id AND ba.security_actor_id=:actor_id
        AND ba.assignment_status='ACTIVE' AND ba.effective_from <= now()
        AND (ba.effective_to IS NULL OR ba.effective_to >= now())
        AND (ba.dealer_id IS NULL
             OR (ba.dealer_id=j.dealer_id AND (ba.outlet_id IS NULL OR ba.outlet_id=j.outlet_id)))
    )
"""


def _require_report_role(connection: Connection, *, tenant_id: str, actor_id: str, functional_role: str | None) -> None:
    if (functional_role or "").upper() in _ADMIN_ROLES:
        return
    held = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.business_assignments
            WHERE tenant_id=:t AND security_actor_id=:a AND assignment_status='ACTIVE'
              AND effective_from <= now() AND (effective_to IS NULL OR effective_to >= now())
              AND upper(business_role_code) = ANY(:roles)
            LIMIT 1
            """
        ),
        {"t": tenant_id, "a": actor_id, "roles": list(_REPORT_ROLES)},
    ).scalar_one_or_none()
    if held is None:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")


def _bounds(connection: Connection, *, tenant_id: str, day: date) -> tuple[str, datetime, datetime]:
    row = connection.execute(
        text(
            """
            SELECT COALESCE(p.timezone_name, :fallback) AS tz,
                   (CAST(:day AS date))::timestamp AT TIME ZONE COALESCE(p.timezone_name, :fallback) AS s,
                   (CAST(:day AS date) + 1)::timestamp AT TIME ZONE COALESCE(p.timezone_name, :fallback) AS e
            FROM (SELECT :t AS tenant_id) x
            LEFT JOIN auditcore.projects p ON p.tenant_id = x.tenant_id
            LIMIT 1
            """
        ),
        {"t": tenant_id, "day": day.isoformat(), "fallback": _FALLBACK_TIMEZONE},
    ).mappings().one()
    return str(row["tz"]), row["s"], row["e"]


# Why a page was rejected at upload, from the document service's failure code
# (2026-10-20: rejections per day show the quality of what is uploaded).
_REJECTION_REASONS = ("faded", "blurred", "tooSmall", "unreadable", "other")


def rejection_reason(error: str | None) -> str | None:
    code = str(error or "").upper()
    if code == "DI_QUALITY_IMAGE_CONTRAST":
        return "faded"
    if code == "DI_QUALITY_IMAGE_BLUR_SCORE":
        return "blurred"
    if code == "DI_QUALITY_IMAGE_MIN_DIMENSIONS":
        return "tooSmall"
    if code in _REJECTED_ERRORS or code == "DI_QUALITY_FILE_NOT_EMPTY":
        return "unreadable"
    if code.startswith("DI_QUALITY_"):
        return "other"
    return None


def _empty_counts() -> dict[str, Any]:
    return {
        "files": 0, "pages": 0, "duplicateFiles": 0,
        "documents": {"extracted": 0, "readNoValues": 0, "notRead": 0, "waiting": 0, "failed": 0, "rejectedScans": 0},
        "rejections": dict.fromkeys(_REJECTION_REASONS, 0),
        "pageMath": {"multiPageDocuments": 0, "pagesInMultiPage": 0, "repeatPages": 0},
        "tasksOpened": 0, "tasksOpenedPc": 0, "tasksOpenedTl": 0, "tasksReopened": 0,
        "autoRetries": 0, "retriesExhausted": 0, "pcRetries": 0, "readAgain": 0, "resyncs": 0,
        "nightRequests": 0, "nightFixed": 0,
    }


def _add(total: dict[str, Any], part: dict[str, Any]) -> None:
    for key, value in part.items():
        if isinstance(value, dict):
            _add(total[key], value)
        elif isinstance(value, int) and not isinstance(value, bool):
            total[key] += value


def build_daily_processing_report(
    connection: Connection, *, tenant_id: str, actor_id: str, day: date,
) -> dict[str, Any]:
    timezone_name, start, end = _bounds(connection, tenant_id=tenant_id, day=day)
    params = {"t": tenant_id, "actor_id": actor_id, "s": start, "e": end}

    # Uploads made on the day, per journey and uploading PC.
    uploads = connection.execute(
        text(
            f"""
            SELECT ub.journey_id, ub.uploaded_by_actor_id AS pc_actor_id,
                   COUNT(*) AS files,
                   COALESCE(SUM(ub.page_count), 0) AS pages,
                   COUNT(*) FILTER (WHERE ub.batch_status='CANCELLED' AND ub.page_count=0
                                     AND ub.sha256 IS NOT NULL) AS duplicate_files,
                   MIN(ub.created_at_utc) AS started_at,
                   array_agg(ub.batch_id) AS batch_ids
            FROM auditcore.p2_upload_batches ub
            JOIN auditcore.journeys j ON j.tenant_id=ub.tenant_id AND j.journey_id=ub.journey_id
            WHERE ub.tenant_id=:t AND ub.created_at_utc >= :s AND ub.created_at_utc < :e
              AND ub.batch_status <> 'AWAITING_UPLOAD' AND {_SCOPE}
            GROUP BY ub.journey_id, ub.uploaded_by_actor_id
            """
        ),
        params,
    ).mappings().all()
    batch_ids = [b for row in uploads for b in row["batch_ids"]]

    # A page the day-end sweep cleared is still a failed document of its upload.
    units = connection.execute(
        text(
            f"""
            SELECT q.batch_id,
                   CASE WHEN {swept_pending_sql("q")} THEN 'FAILED' ELSE q.queue_status END AS queue_status,
                   q.last_error, q.unit_kind,
                   COALESCE(cardinality(q.page_numbers), 1) AS unit_pages
            FROM auditcore.p2_document_queue q
            WHERE q.tenant_id=:t AND q.batch_id = ANY(:batches)
              AND q.queue_status <> 'MERGED'
            """
        ),
        {"t": tenant_id, "batches": batch_ids},
    ).mappings().all() if batch_ids else []
    settled_at = dict(connection.execute(
        text(
            """
            SELECT (e.details->>'batchId')::uuid AS batch_id, MAX(e.created_at_utc) AS settled_at
            FROM auditcore.p2_activity_events e
            WHERE e.tenant_id=:t AND e.event_type='DOCUMENT_STATUS_CHANGED'
              AND e.details->>'batchId' = ANY(:batches)
              AND e.details->>'status' = ANY(:settled)
            GROUP BY 1
            """
        ),
        {"t": tenant_id, "batches": [str(b) for b in batch_ids], "settled": list(_SETTLED)},
    ).all()) if batch_ids else {}

    by_batch: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        by_batch[unit["batch_id"]].append(dict(unit))

    # What happened on the day, per journey, from the activity history.
    events = connection.execute(
        text(
            f"""
            SELECT e.journey_id, e.event_type, e.details->>'status' AS status, e.details->>'by' AS by_whom
            FROM auditcore.p2_activity_events e
            JOIN auditcore.journeys j ON j.tenant_id=e.tenant_id AND j.journey_id=e.journey_id
            WHERE e.tenant_id=:t AND e.created_at_utc >= :s AND e.created_at_utc < :e AND {_SCOPE}
              AND (e.event_type IN ('P2_WORK_DEAD_LETTER','PAGE_RETRY_REQUESTED','PAGE_REREAD_REQUESTED',
                                    'RECHECK_REQUESTED')
                   OR (e.event_type='DOCUMENT_STATUS_CHANGED' AND e.details->>'status'='RETRY_WAIT'))
            """
        ),
        params,
    ).mappings().all()
    night_fixed = connection.execute(
        text(
            f"""
            SELECT r.journey_id, COUNT(DISTINCT r.subject_id) AS fixed
            FROM auditcore.p2_activity_events r
            JOIN auditcore.journeys j ON j.tenant_id=r.tenant_id AND j.journey_id=r.journey_id
            WHERE r.tenant_id=:t AND r.created_at_utc >= :s AND r.created_at_utc < :e AND {_SCOPE}
              AND ((r.event_type='PAGE_RETRY_REQUESTED' AND r.details->>'by'='nightly sweep')
                   OR (r.event_type='PAGE_REREAD_REQUESTED' AND r.details->>'by'='SYSTEM'))
              AND EXISTS (
                SELECT 1 FROM auditcore.p2_activity_events ok
                WHERE ok.tenant_id=r.tenant_id AND ok.journey_id=r.journey_id
                  AND ok.subject_id=r.subject_id AND ok.event_id > r.event_id
                  AND ok.event_type='DOCUMENT_STATUS_CHANGED' AND ok.details->>'status'='READY')
            GROUP BY r.journey_id
            """
        ),
        params,
    ).mappings().all()
    tasks = connection.execute(
        text(
            f"""
            SELECT x.journey_id, x.kind, x.role, COUNT(*) AS n FROM (
              SELECT tk.journey_id, 'OPENED' AS kind, upper(COALESCE(tk.assigned_role_code,'')) AS role
              FROM auditcore.p2_tasks tk
              JOIN auditcore.journeys j ON j.tenant_id=tk.tenant_id AND j.journey_id=tk.journey_id
              WHERE tk.tenant_id=:t AND tk.created_at_utc >= :s AND tk.created_at_utc < :e AND {_SCOPE}
              UNION ALL
              SELECT tk.journey_id, 'REOPENED', upper(COALESCE(tk.assigned_role_code,''))
              FROM auditcore.p2_task_events te
              JOIN auditcore.p2_tasks tk ON tk.tenant_id=te.tenant_id AND tk.task_id=te.task_id
              JOIN auditcore.journeys j ON j.tenant_id=tk.tenant_id AND j.journey_id=tk.journey_id
              WHERE te.tenant_id=:t AND te.event_type='REOPENED'
                AND te.created_at_utc >= :s AND te.created_at_utc < :e AND {_SCOPE}
            ) x GROUP BY 1, 2, 3
            """
        ),
        params,
    ).mappings().all()

    journey_ids = (
        {row["journey_id"] for row in uploads} | {row["journey_id"] for row in events}
        | {row["journey_id"] for row in night_fixed} | {row["journey_id"] for row in tasks}
    )
    info = {
        row["journey_id"]: dict(row)
        for row in connection.execute(
            text(
                """
                SELECT j.journey_id, COALESCE(c.legal_name, c.display_name) AS customer_name,
                       j.created_by_actor_id, j.created_by_display_name,
                       o.outlet_name, o.outlet_code, d.dealer_name
                FROM auditcore.journeys j
                JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
                JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
                JOIN auditcore.dealer_outlets o
                  ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
                WHERE j.tenant_id=:t AND j.journey_id = ANY(:ids)
                """
            ),
            {"t": tenant_id, "ids": list(journey_ids)},
        ).mappings()
    } if journey_ids else {}
    # A PC's name is what their app recorded when they started a journey.
    names = dict(connection.execute(
        text(
            """
            SELECT DISTINCT ON (created_by_actor_id) created_by_actor_id, created_by_display_name
            FROM auditcore.journeys
            WHERE tenant_id=:t AND created_by_actor_id IS NOT NULL AND created_by_display_name IS NOT NULL
            ORDER BY created_by_actor_id, created_at_utc DESC
            """
        ),
        {"t": tenant_id},
    ).all())

    rows: dict[tuple[Any, str], dict[str, Any]] = {}

    def row_for(journey_id: Any, pc_actor_id: str | None) -> dict[str, Any]:
        pc = pc_actor_id or (info.get(journey_id) or {}).get("created_by_actor_id") or ""
        key = (journey_id, pc)
        if key not in rows:
            meta = info.get(journey_id) or {}
            rows[key] = {
                "journeyId": str(journey_id),
                "customerName": meta.get("customer_name"),
                "outletName": meta.get("outlet_name"),
                "outletCode": meta.get("outlet_code"),
                "dealerName": meta.get("dealer_name"),
                "pcActorId": pc or None,
                "pcName": names.get(pc) or None,
                "uploadStartedAt": None,
                "processingFinishedAt": None,
                "processingMinutes": None,
                **_empty_counts(),
            }
        return rows[key]

    for upload in uploads:
        row = row_for(upload["journey_id"], upload["pc_actor_id"])
        row["files"] += int(upload["files"])
        row["pages"] += int(upload["pages"])
        row["duplicateFiles"] += int(upload["duplicate_files"])
        row["uploadStartedAt"] = upload["started_at"]
        in_flight = False
        finished: datetime | None = None
        for batch_id in upload["batch_ids"]:
            for unit in by_batch.get(batch_id, []):
                status = str(unit["queue_status"])
                docs = row["documents"]
                # How the pages became documents (2026-10-20: "48 pages but
                # 41 documents, where are the other pages?"): the pages of
                # a multi-page document make one document, and a page
                # repeated within the upload is dropped.
                if status == "CANCELLED":
                    row["pageMath"]["repeatPages"] += 1
                    continue
                if str(unit["unit_kind"]) == "GROUP":
                    row["pageMath"]["multiPageDocuments"] += 1
                    row["pageMath"]["pagesInMultiPage"] += int(unit["unit_pages"] or 1)
                if status == "READY":
                    docs["extracted"] += 1
                elif status == "NEEDS_REVIEW":
                    docs["readNoValues"] += 1
                elif status == "SUPPORTING":
                    docs["notRead"] += 1
                elif status in _WAITING:
                    docs["waiting"] += 1
                    in_flight = True
                elif status in ("FAILED", "DEAD_LETTER"):
                    docs["failed"] += 1
                    error = str(unit["last_error"] or "")
                    reason = rejection_reason(error)
                    if reason is not None:
                        docs["rejectedScans"] += 1
                        row["rejections"][reason] += 1
            at = settled_at.get(batch_id)
            if at is not None and (finished is None or at > finished):
                finished = at
        if not in_flight and finished is not None:
            row["processingFinishedAt"] = finished
            row["processingMinutes"] = max(0, round((finished - upload["started_at"]).total_seconds() / 60))

    for event in events:
        row = row_for(event["journey_id"], None)
        kind, by_whom = event["event_type"], event["by_whom"]
        if kind == "DOCUMENT_STATUS_CHANGED":
            row["autoRetries"] += 1
        elif kind == "P2_WORK_DEAD_LETTER":
            row["retriesExhausted"] += 1
        elif kind == "PAGE_RETRY_REQUESTED":
            if by_whom == "nightly sweep":
                row["nightRequests"] += 1
            else:
                row["pcRetries"] += 1
        elif kind == "PAGE_REREAD_REQUESTED":
            if by_whom == "SYSTEM":
                row["nightRequests"] += 1
            else:
                row["readAgain"] += 1
        elif kind == "RECHECK_REQUESTED":
            row["resyncs"] += 1
    for fixed in night_fixed:
        row_for(fixed["journey_id"], None)["nightFixed"] += int(fixed["fixed"])
    for task in tasks:
        row = row_for(task["journey_id"], None)
        n = int(task["n"])
        if task["kind"] == "REOPENED":
            row["tasksReopened"] += n
            continue
        row["tasksOpened"] += n
        if task["role"] == "PC":
            row["tasksOpenedPc"] += n
        elif task["role"] in ("TL", "PM"):
            row["tasksOpenedTl"] += n

    journeys = sorted(rows.values(), key=lambda r: (r["pcName"] or r["pcActorId"] or "~", r["uploadStartedAt"] or end))
    pcs: dict[str, dict[str, Any]] = {}
    for row in journeys:
        pc = row["pcActorId"] or ""
        summary = pcs.setdefault(pc, {
            "pcActorId": row["pcActorId"], "pcName": row["pcName"], "journeys": 0, "journeysUploaded": 0,
            "firstUploadAt": None, "lastFinishedAt": None, "stillProcessing": 0, **_empty_counts(),
            "earlier": {"journeys": 0, **_empty_counts()},
        })
        summary["journeys"] += 1
        counts = {k: v for k, v in row.items() if k in _empty_counts()}
        row["uploadedOnDay"] = row["uploadStartedAt"] is not None
        if row["uploadStartedAt"] is None:
            # 2026-10-20: a journey the PC did not upload on the day (tasks the
            # system's own re-checks raised, night fixes) is not the PC's work
            # of the day -- it is reported apart, never added to the day's row.
            summary["earlier"]["journeys"] += 1
            _add(summary["earlier"], counts)
            continue
        _add(summary, counts)
        if row["uploadStartedAt"] is not None:
            summary["journeysUploaded"] += 1
            if summary["firstUploadAt"] is None or row["uploadStartedAt"] < summary["firstUploadAt"]:
                summary["firstUploadAt"] = row["uploadStartedAt"]
            if row["processingFinishedAt"] is None:
                summary["stillProcessing"] += 1
            elif summary["lastFinishedAt"] is None or row["processingFinishedAt"] > summary["lastFinishedAt"]:
                summary["lastFinishedAt"] = row["processingFinishedAt"]
    for summary in pcs.values():
        uploaded = summary["journeysUploaded"]
        summary["pagesPerJourney"] = round(summary["pages"] / uploaded, 1) if uploaded else None
        start_at, finish_at = summary["firstUploadAt"], summary["lastFinishedAt"]
        summary["spanMinutes"] = (
            max(0, round((finish_at - start_at).total_seconds() / 60))
            if start_at is not None and finish_at is not None and not summary["stillProcessing"] else None
        )

    day_total = _empty_counts()
    for row in journeys:
        if row["uploadStartedAt"] is not None:
            _add(day_total, {k: v for k, v in row.items() if k in _empty_counts()})
    docs = day_total["documents"]
    # Every page and file that failed on the day, with its reason and a link
    # that opens it (product owner 2026-10-21).
    failed = build_failed_pages_report(connection, tenant_id=tenant_id, actor_id=actor_id, start=start, end=end)
    documents = docs["extracted"] + docs["readNoValues"] + docs["notRead"] + docs["waiting"] + docs["failed"]
    file_list = _uploaded_files(
        connection, tenant_id=tenant_id, actor_id=actor_id, start=start, end=end, names=names,
        units_by_batch=by_batch,
    )
    status = _document_status(file_list["files"], pcs, names)
    team = _team_work(connection, tenant_id=tenant_id, actor_id=actor_id, start=start, end=end, names=names)
    large = [f for f in file_list["files"] if f["large"]]
    return {
        "day": day.isoformat(),
        "timezone": timezone_name,
        "documentStatus": status,
        "teamWork": team,
        "files": file_list["files"],
        "filesTruncated": file_list["truncated"],
        "linkExpiresInSeconds": _LINK_SECONDS,
        "largeUploads": {"threshold": LARGE_UPLOAD_PAGES, "count": len(large), "files": large},
        "quality": {
            "files": day_total["files"],
            "pages": day_total["pages"],
            "documents": documents,
            "rejected": docs["rejectedScans"],
            "rejectedPct": round(100.0 * docs["rejectedScans"] / documents, 1) if documents else None,
            "byReason": day_total["rejections"],
        },
        "trend": _rejection_trend(connection, params=params, timezone_name=timezone_name, day=day),
        # PCs who uploaded on the day first, the busiest first.
        "pcs": sorted(pcs.values(), key=lambda s: (-s["journeysUploaded"], s["pcName"] or s["pcActorId"] or "~")),
        "journeys": journeys,
        "failedPages": failed["rows"],
        "failedPagesTruncated": failed["truncated"],
        "notes": [
            "Documents are the units the uploads became after splitting and grouping, in their state now.",
            "Finished is the last document settling; it stays blank while any document of that day's uploads is in flight.",
            ("A PC's row counts only the journeys they uploaded on the day. Tasks the system's own re-checks "
             "raised that day on their earlier journeys (night re-sync, SKU re-runs, rule changes) are shown "
             "apart as 'Earlier journeys'."),
            ("Rejected at upload: pages the scan-quality check refused (faded, blurred, too small or "
             "unreadable) -- never read, the PC is asked to re-scan. The trend counts the last 14 days."),
            "Re-sync counts the journey's 'Re-sync with Document Intelligence'; who pressed it is not stored.",
            ("Night jobs run at about 02:00 local time; night-fixed counts pages the sweep asked for that were "
             "read afterwards. Since 2026-10-22 nothing is read again at night, so both counts are zero for "
             "later days; days before keep their figures."),
            ("Failed pages lists every page and file that failed on the day and is still failed, with the reason "
             "and a link that opens it (valid 30 minutes from when the report was loaded)."),
            "Document Intelligence's own nightly re-processing keeps its per-document results in DI and is not counted here.",
            ("Document status: rejected = pages the scan-quality check refused; retried = automatic re-attempts and "
             "the PC's own retry / read-again requests; not uploaded = files that never arrived within 30 minutes "
             "(the PC is told to upload again); duplicate files are dropped on arrival."),
            (f"Large uploads: a file of more than {LARGE_UPLOAD_PAGES} pages gets the PC a notice to review it and "
             "confirm the pages are needed. Nothing is blocked; the PC closes the notice with a comment, shown here."),
            ("Work done counts what people did on tasks that day (comments, reviews, uploads, closing). The "
             "system's own task updates are not work. Tasks closed = worked by that person and closed on the day. "
             "A Team Lead's name is not stored with the task, so the account id shows unless a name is known."),
            "File links open the original as uploaded and are valid 30 minutes from when the report was loaded.",
        ],
    }


def _file_state(row: Any, units: list[dict[str, Any]]) -> str:
    """Where one uploaded file stands, in one word."""
    from audit_core.uc03_p2_task_producer import FILE_NOT_UPLOADED_IN_TIME_REASON

    status = str(row["batch_status"])
    pages = int(row["page_count"] or 0)
    if status == "AWAITING_UPLOAD":
        return "UPLOAD_PENDING"
    if status == "FAILED" and pages == 0:
        return "NOT_UPLOADED" if (row["failure_reason"] or "") == FILE_NOT_UPLOADED_IN_TIME_REASON else "NOT_SPLIT"
    if status == "CANCELLED" and pages == 0:
        return "DUPLICATE" if row["sha256"] else "CANCELLED"
    states = [str(u["queue_status"]) for u in units if str(u["queue_status"]) != "CANCELLED"]
    if any(st in _WAITING for st in states):
        return "READING"
    if any(st in ("FAILED", "DEAD_LETTER") for st in states):
        return "PARTLY_FAILED"
    return "READ"


def _uploaded_files(
    connection: Connection, *, tenant_id: str, actor_id: str, start: datetime, end: datetime,
    names: dict[str, str], units_by_batch: dict[Any, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Every file uploaded on the day (any state, a file that never arrived
    included), newest first, with a link that opens the original and the
    large-upload flag. The PC is the uploader, else the journey's PC."""
    from audit_core.uc03_p2_task_producer import upload_open_path

    rows = connection.execute(
        text(
            f"""
            SELECT b.batch_id, b.journey_id, j.journey_reference, b.original_filename, b.page_count,
                   b.batch_status, b.failure_reason, b.sha256, b.original_object_key, b.created_at_utc,
                   COALESCE(b.uploaded_by_actor_id, j.created_by_actor_id) AS pc_actor_id,
                   COALESCE(c.legal_name, c.display_name) AS customer_name,
                   d.dealer_name, o.outlet_name, o.outlet_code,
                   tk.task_status AS review_status,
                   (SELECT te.comment FROM auditcore.p2_task_events te
                     WHERE te.tenant_id=tk.tenant_id AND te.task_id=tk.task_id
                       AND te.event_type IN ('CLOSE_TASK','ADD_COMMENT','PROVIDE_FEEDBACK')
                       AND te.comment IS NOT NULL
                     ORDER BY te.created_at_utc DESC LIMIT 1) AS review_comment
            FROM auditcore.p2_upload_batches b
            JOIN auditcore.journeys j ON j.tenant_id=b.tenant_id AND j.journey_id=b.journey_id
            LEFT JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            LEFT JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
            LEFT JOIN auditcore.dealer_outlets o
              ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
            LEFT JOIN auditcore.p2_tasks tk
              ON tk.tenant_id=b.tenant_id AND tk.journey_id=b.journey_id
             AND tk.dedupe_key = 'large-upload:' || b.journey_id::text || ':' || b.batch_id::text
            WHERE b.tenant_id=:t AND b.created_at_utc >= :s AND b.created_at_utc < :e AND {_SCOPE}
            ORDER BY b.created_at_utc DESC
            LIMIT :limit
            """
        ),
        {"t": tenant_id, "actor_id": actor_id, "s": start, "e": end, "limit": _FILE_ROWS_LIMIT + 1},
    ).mappings().all()
    truncated = len(rows) > _FILE_ROWS_LIMIT
    rows = rows[:_FILE_ROWS_LIMIT]
    batch_ids = [r["batch_id"] for r in rows if r["batch_id"] not in (units_by_batch or {})]
    units_by_batch = dict(units_by_batch or {})
    if batch_ids:
        extra: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for unit in connection.execute(
            text(
                f"""
                SELECT q.batch_id,
                       CASE WHEN {swept_pending_sql("q")} THEN 'FAILED' ELSE q.queue_status END AS queue_status,
                       q.last_error, q.unit_kind, COALESCE(cardinality(q.page_numbers), 1) AS unit_pages
                FROM auditcore.p2_document_queue q
                WHERE q.tenant_id=:t AND q.batch_id = ANY(:batches) AND q.queue_status <> 'MERGED'
                """
            ),
            {"t": tenant_id, "batches": batch_ids},
        ).mappings():
            extra[unit["batch_id"]].append(dict(unit))
        units_by_batch.update(extra)

    files: list[dict[str, Any]] = []
    for row in rows:
        units = units_by_batch.get(row["batch_id"], [])
        state = _file_state(row, units)
        pages = int(row["page_count"] or 0)
        docs = {"read": 0, "waiting": 0, "failed": 0, "rejected": 0}
        for unit in units:
            status = str(unit["queue_status"])
            if status in ("READY", "NEEDS_REVIEW", "SUPPORTING"):
                docs["read"] += 1
            elif status in _WAITING:
                docs["waiting"] += 1
            elif status in ("FAILED", "DEAD_LETTER"):
                docs["failed"] += 1
                if rejection_reason(unit["last_error"]) is not None:
                    docs["rejected"] += 1
        has_file = state not in ("UPLOAD_PENDING", "NOT_UPLOADED") and bool(row["original_object_key"])
        large = pages > LARGE_UPLOAD_PAGES and state not in ("CANCELLED", "NOT_SPLIT", "NOT_UPLOADED", "UPLOAD_PENDING")
        pc = row["pc_actor_id"] or ""
        files.append({
            "batchId": str(row["batch_id"]), "journeyId": str(row["journey_id"]),
            "journeyReference": row["journey_reference"], "customerName": row["customer_name"],
            "dealerName": row["dealer_name"], "outletName": row["outlet_name"], "outletCode": row["outlet_code"],
            "pcActorId": pc or None, "pcName": names.get(pc),
            "filename": row["original_filename"], "pages": pages, "state": state,
            "failureReason": row["failure_reason"] if state in ("NOT_UPLOADED", "NOT_SPLIT") else None,
            "uploadedAtUtc": row["created_at_utc"].isoformat() if row["created_at_utc"] else None,
            "documents": docs,
            "large": large,
            "review": ({"status": "OPEN" if str(row["review_status"]) not in ("VERIFIED_COMPLETE", "CANCELLED")
                        else "CLOSED", "comment": row["review_comment"]}
                       if large and row["review_status"] else None),
            # A file that never arrived has nothing to open.
            "openUrl": _signed_link(row["original_object_key"]) if has_file else None,
            "filePath": upload_open_path(tenant_id, row["journey_id"], row["batch_id"]) if has_file else None,
        })
    return {"files": files, "truncated": truncated}


def _document_status(
    files: list[dict[str, Any]], pcs: dict[str, dict[str, Any]], names: dict[str, str],
) -> dict[str, Any]:
    """The day's document status in the words the product owner uses
    (2026-10-26): uploaded, not uploaded, rejected, retried, read, waiting --
    per PC and in total. Files are counted from the file list; documents and
    retries from each PC's row of the report."""
    def empty() -> dict[str, Any]:
        return {
            "filesUploaded": 0, "filesNotUploaded": 0, "filesUploadPending": 0, "filesNotSplit": 0,
            "duplicateFiles": 0, "largeUploads": 0,
            "documents": 0, "read": 0, "rejected": 0, "failedOther": 0, "waiting": 0,
            "retriedAutomatically": 0, "retriedByPc": 0, "retriesExhausted": 0,
        }

    by_pc: dict[str, dict[str, Any]] = {}

    def slot(pc: str) -> dict[str, Any]:
        if pc not in by_pc:
            by_pc[pc] = {"pcActorId": pc or None, "pcName": names.get(pc) or (pcs.get(pc) or {}).get("pcName"),
                         **empty()}
        return by_pc[pc]

    for file in files:
        entry = slot(file["pcActorId"] or "")
        state = file["state"]
        if state == "NOT_UPLOADED":
            entry["filesNotUploaded"] += 1
        elif state == "UPLOAD_PENDING":
            entry["filesUploadPending"] += 1
        elif state == "NOT_SPLIT":
            entry["filesNotSplit"] += 1
        elif state == "DUPLICATE":
            entry["duplicateFiles"] += 1
        elif state != "CANCELLED":
            entry["filesUploaded"] += 1
        if file["large"]:
            entry["largeUploads"] += 1
    for pc, summary in pcs.items():
        entry = slot(pc)
        docs = summary["documents"]
        entry["read"] = docs["extracted"] + docs["readNoValues"] + docs["notRead"]
        entry["rejected"] = docs["rejectedScans"]
        entry["failedOther"] = max(0, docs["failed"] - docs["rejectedScans"])
        entry["waiting"] = docs["waiting"]
        entry["documents"] = entry["read"] + docs["failed"] + docs["waiting"]
        entry["retriedAutomatically"] = summary["autoRetries"]
        entry["retriedByPc"] = summary["pcRetries"] + summary["readAgain"]
        entry["retriesExhausted"] = summary["retriesExhausted"]
    total = empty()
    for entry in by_pc.values():
        for key in total:
            total[key] += entry[key]
    people = sorted(by_pc.values(), key=lambda e: (-e["filesUploaded"], e["pcName"] or e["pcActorId"] or "~"))
    return {"total": total, "byPc": people}


def _team_work(
    connection: Connection, *, tenant_id: str, actor_id: str, start: datetime, end: datetime,
    names: dict[str, str],
) -> dict[str, Any]:
    """What each PC / Team Lead did on tasks during the day: the tasks they
    worked (a task counts once however many actions), the actions, the
    journeys, and which of those tasks closed on the day. Machine events
    (SYSTEM) are not work. A Team Lead's name is not stored with the task, so
    it shows as the account id unless that person also started journeys."""
    rows = connection.execute(
        text(
            f"""
            SELECT te.actor_id, upper(COALESCE(te.actor_role_code, '')) AS role, te.event_type, te.task_id,
                   te.journey_id, te.created_at_utc, te.comment,
                   tk.task_type, tk.title, tk.task_status, tk.verified_at_utc,
                   j.journey_reference, COALESCE(c.legal_name, c.display_name) AS customer_name
            FROM auditcore.p2_task_events te
            JOIN auditcore.p2_tasks tk ON tk.tenant_id=te.tenant_id AND tk.task_id=te.task_id
            JOIN auditcore.journeys j ON j.tenant_id=tk.tenant_id AND j.journey_id=tk.journey_id
            LEFT JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            WHERE te.tenant_id=:t AND te.created_at_utc >= :s AND te.created_at_utc < :e
              AND te.actor_id IS NOT NULL AND upper(te.actor_id) <> 'SYSTEM'
              AND te.event_type = ANY(:work) AND {_SCOPE}
            ORDER BY te.created_at_utc
            """
        ),
        {"t": tenant_id, "actor_id": actor_id, "s": start, "e": end, "work": list(_WORK_EVENTS)},
    ).mappings().all()
    people: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["actor_id"]), row["role"])
        person = people.setdefault(key, {
            "actorId": key[0], "name": names.get(key[0]), "role": key[1] or None,
            "actions": 0, "byAction": {}, "firstAt": None, "lastAt": None,
            "_tasks": {}, "_journeys": set(),
        })
        person["actions"] += 1
        person["byAction"][row["event_type"]] = person["byAction"].get(row["event_type"], 0) + 1
        at = row["created_at_utc"]
        person["firstAt"] = person["firstAt"] or at
        person["lastAt"] = at
        person["_journeys"].add(row["journey_id"])
        task = person["_tasks"].setdefault(row["task_id"], {
            "taskId": str(row["task_id"]), "journeyId": str(row["journey_id"]),
            "journeyReference": row["journey_reference"], "customerName": row["customer_name"],
            "taskType": row["task_type"], "title": row["title"], "status": row["task_status"],
            "closedOnDay": bool(row["verified_at_utc"] and start <= row["verified_at_utc"] < end
                                and row["task_status"] == "VERIFIED_COMPLETE"),
            "actions": [], "lastAt": None, "lastComment": None,
        })
        if row["event_type"] not in task["actions"]:
            task["actions"].append(row["event_type"])
        task["lastAt"] = at.isoformat()
        if row["comment"]:
            task["lastComment"] = row["comment"]
    out: list[dict[str, Any]] = []
    for person in people.values():
        tasks = list(person.pop("_tasks").values())
        by_type: dict[str, int] = {}
        for task in tasks:
            by_type[task["taskType"]] = by_type.get(task["taskType"], 0) + 1
        person["tasksWorked"] = len(tasks)
        person["tasksClosed"] = sum(1 for t in tasks if t["closedOnDay"])
        person["journeys"] = len(person.pop("_journeys"))
        person["byTaskType"] = by_type
        tasks.sort(key=lambda t: t["lastAt"] or "", reverse=True)
        person["tasks"] = tasks[:_WORK_TASKS_PER_PERSON]
        person["tasksTruncated"] = len(tasks) > _WORK_TASKS_PER_PERSON
        person["firstAt"] = person["firstAt"].isoformat() if person["firstAt"] else None
        person["lastAt"] = person["lastAt"].isoformat() if person["lastAt"] else None
        out.append(person)
    order = {"PC": 0, "TL": 1, "PM": 2}
    out.sort(key=lambda p: (order.get(p["role"] or "", 9), -p["tasksWorked"], p["name"] or p["actorId"]))
    return {
        "people": out,
        "totals": {
            "people": len(out), "tasksWorked": sum(p["tasksWorked"] for p in out),
            "actions": sum(p["actions"] for p in out), "tasksClosed": sum(p["tasksClosed"] for p in out),
        },
    }


def _rejection_trend(
    connection: Connection, *, params: dict[str, Any], timezone_name: str, day: date, days: int = 14,
) -> list[dict[str, Any]]:
    """Pages uploaded and rejected at upload per day, the ``days`` days up to
    and including ``day``, by the day of the upload in the Project's timezone."""
    rows = connection.execute(
        text(
            f"""
            SELECT (ub.created_at_utc AT TIME ZONE :tz)::date AS day,
                   q.queue_status, q.last_error, count(*) AS n
            FROM auditcore.p2_upload_batches ub
            JOIN auditcore.journeys j ON j.tenant_id=ub.tenant_id AND j.journey_id=ub.journey_id
            JOIN auditcore.p2_document_queue q ON q.tenant_id=ub.tenant_id AND q.batch_id=ub.batch_id
            WHERE ub.tenant_id=:t AND ub.batch_status <> 'AWAITING_UPLOAD'
              AND ub.created_at_utc < :e
              AND ub.created_at_utc >= :e - make_interval(days => :days)
              AND q.queue_status NOT IN ('MERGED','CANCELLED')
              AND {_SCOPE}
            GROUP BY 1, 2, 3
            """
        ),
        {**params, "tz": timezone_name, "days": days},
    ).mappings().all()
    by_day: dict[str, dict[str, Any]] = {}
    for offset in range(days - 1, -1, -1):
        key = date.fromordinal(day.toordinal() - offset).isoformat()
        by_day[key] = {"day": key, "documents": 0, "rejected": 0, "byReason": dict.fromkeys(_REJECTION_REASONS, 0)}
    for row in rows:
        entry = by_day.get(row["day"].isoformat())
        if entry is None:
            continue
        n = int(row["n"])
        entry["documents"] += n
        reason = (rejection_reason(row["last_error"])
                  if row["queue_status"] in ("FAILED", "DEAD_LETTER") else None)
        if reason is not None:
            entry["rejected"] += n
            entry["byReason"][reason] += n
    for entry in by_day.values():
        entry["rejectedPct"] = round(100.0 * entry["rejected"] / entry["documents"], 1) if entry["documents"] else None
    return list(by_day.values())


# The failure report (product owner 2026-10-21): every page that failed, and
# every file that failed as a whole, with the reason in plain words and a
# link that opens it from storage. The link is a short-lived presigned link
# issued with the report (the same mechanism the app serves documents by,
# never a raw bucket address); filePath is the app endpoint that issues a
# fresh one later (the PC's resubmit task carries that path).
_FAILED_ROWS_LIMIT = 1000
_LINK_SECONDS = 1800


def _signed_link(object_key: str | None) -> str | None:
    if not object_key:
        return None
    from audit_core.uc03_p2_storage import (
        P2DocumentStorageError,
        get_p2_document_storage,
    )

    try:
        return get_p2_document_storage().presign_get(object_key, expires_seconds=_LINK_SECONDS)
    except (RuntimeError, P2DocumentStorageError):
        return None


def build_failed_pages_report(
    connection: Connection, *, tenant_id: str, actor_id: str, start: datetime, end: datetime,
    limit: int = _FAILED_ROWS_LIMIT,
) -> dict[str, Any]:
    """Every page (document unit) now FAILED whose failure falls in [start,
    end), and every file that failed before it was split into pages, within
    the reader's own dealers and outlets. Newest failure first. A page the
    day-end sweep cleared from the Journey (2026-10-22) is still listed, with
    its reason and a link to its stored file, until it is resubmitted. A page
    the PC uploaded again (superseded), removed or read again is no longer
    failed and is not listed."""
    from audit_core.uc03_p2_task_producer import (
        FILE_NOT_UPLOADED_IN_TIME_REASON,
        SETUP_FAILURE_CODE,
        page_open_path,
    )

    params = {"t": tenant_id, "actor_id": actor_id, "s": start, "e": end, "limit": limit + 1}
    pages = connection.execute(
        text(
            f"""
            SELECT * FROM (
              SELECT q.queue_id, q.journey_id, j.journey_reference,
                     COALESCE(c.legal_name, c.display_name) AS customer_name,
                     d.dealer_name, o.outlet_name, o.outlet_code,
                     b.batch_id, b.original_filename, b.uploaded_by_actor_id,
                     b.created_at_utc AS uploaded_at,
                     COALESCE(q.page_numbers, ARRAY[q.page_number]) AS page_numbers,
                     q.attempt_count, q.status_reason, q.last_error, q.page_object_key,
                     q.queue_status AS unit_status,
                     COALESCE(
                       (SELECT MAX(e.created_at_utc) FROM auditcore.p2_activity_events e
                        WHERE e.tenant_id=q.tenant_id AND e.journey_id=q.journey_id
                          AND e.subject_id=q.queue_id::text AND e.event_type='DOCUMENT_STATUS_CHANGED'
                          AND e.details->>'status' IN ('FAILED','DEAD_LETTER')),
                       q.updated_at_utc) AS failed_at
              FROM auditcore.p2_document_queue q
              JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
              JOIN auditcore.journeys j ON j.tenant_id=q.tenant_id AND j.journey_id=q.journey_id
              LEFT JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
              LEFT JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
              LEFT JOIN auditcore.dealer_outlets o
                ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
              WHERE q.tenant_id=:t
                AND (q.queue_status IN ('FAILED','DEAD_LETTER') OR {swept_pending_sql("q")})
                AND {_SCOPE}
            ) x
            WHERE x.failed_at >= :s AND x.failed_at < :e
            ORDER BY x.failed_at DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    files = connection.execute(
        text(
            f"""
            SELECT b.batch_id, b.journey_id, j.journey_reference,
                   COALESCE(c.legal_name, c.display_name) AS customer_name,
                   d.dealer_name, o.outlet_name, o.outlet_code,
                   b.original_filename, b.uploaded_by_actor_id, b.created_at_utc AS uploaded_at,
                   b.updated_at_utc AS failed_at, b.failure_reason, b.original_object_key
            FROM auditcore.p2_upload_batches b
            JOIN auditcore.journeys j ON j.tenant_id=b.tenant_id AND j.journey_id=b.journey_id
            LEFT JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            LEFT JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
            LEFT JOIN auditcore.dealer_outlets o
              ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
            WHERE b.tenant_id=:t AND b.batch_status='FAILED' AND b.page_count=0
              AND b.updated_at_utc >= :s AND b.updated_at_utc < :e AND {_SCOPE}
            ORDER BY b.updated_at_utc DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()

    def journey(row: Any) -> dict[str, Any]:
        return {
            "journeyId": str(row["journey_id"]), "journeyReference": row["journey_reference"],
            "customerName": row["customer_name"], "dealerName": row["dealer_name"],
            "outletName": row["outlet_name"], "outletCode": row["outlet_code"],
            "batchId": str(row["batch_id"]), "filename": row["original_filename"],
            "uploadedBy": row["uploaded_by_actor_id"],
            "uploadedAtUtc": row["uploaded_at"].isoformat() if row["uploaded_at"] else None,
            "failedAtUtc": row["failed_at"].isoformat() if row["failed_at"] else None,
        }

    rows: list[dict[str, Any]] = []
    for row in pages:
        setup = str(row["last_error"] or "") == SETUP_FAILURE_CODE
        rows.append({
            **journey(row), "kind": "PAGE", "queueId": str(row["queue_id"]),
            "pageNumbers": [int(p) for p in (row["page_numbers"] or [])],
            "attempts": int(row["attempt_count"] or 0) or None,
            "reason": row["status_reason"] or "It could not be read.",
            "code": row["last_error"], "setupProblem": setup,
            # CANCELLED: the day-end sweep cleared it from the Journey; it is
            # listed until it is resubmitted. The file in storage is kept.
            "clearedFromJourney": row["unit_status"] == "CANCELLED",
            "openUrl": _signed_link(row["page_object_key"]),
            "filePath": page_open_path(tenant_id, row["journey_id"], row["queue_id"]),
        })
    for row in files:
        not_uploaded = (row["failure_reason"] or "") == FILE_NOT_UPLOADED_IN_TIME_REASON
        rows.append({
            **journey(row), "kind": "FILE", "queueId": None, "pageNumbers": [], "attempts": None,
            "reason": row["failure_reason"] or "The file could not be split into pages.",
            "code": None, "setupProblem": False, "clearedFromJourney": False,
            # A file that never finished uploading has nothing to open.
            "openUrl": None if not_uploaded else _signed_link(row["original_object_key"]),
            "filePath": None,
        })
    rows.sort(key=lambda r: r["failedAtUtc"] or "", reverse=True)
    truncated = len(rows) > limit
    return {"rows": rows[:limit], "truncated": truncated, "linkExpiresInSeconds": _LINK_SECONDS}


@router.get("/reports/daily-processing")
def get_daily_processing_report(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    day: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    access = authorize_p2(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    _require_report_role(
        connection, tenant_id=tenant_id, actor_id=human_principal.subject,
        functional_role=access.functional_role,
    )
    if day is None:
        timezone_name = connection.execute(
            text("SELECT COALESCE(timezone_name, :f) FROM auditcore.projects WHERE tenant_id=:t"),
            {"t": tenant_id, "f": _FALLBACK_TIMEZONE},
        ).scalar_one_or_none() or _FALLBACK_TIMEZONE
        day = connection.execute(
            text("SELECT (now() AT TIME ZONE :tz)::date"), {"tz": timezone_name},
        ).scalar_one()
    return build_daily_processing_report(connection, tenant_id=tenant_id, actor_id=human_principal.subject, day=day)


@router.get("/reports/failed-pages")
def get_failed_pages_report(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    from_day: Annotated[date | None, Query(alias="from")] = None,
    to_day: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    """The failure report (product owner 2026-10-21): every failed page and
    file of the Project (the tenant in the path is the Project) whose
    failure falls between ``from`` and ``to`` (days, inclusive, in the
    Project's timezone; today when left out): journey, file, page, uploaded
    at, failed at, attempts, reason and a link that opens it. Same readers
    as the daily processing report (TL, PM, Executive)."""
    access = authorize_p2(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    _require_report_role(
        connection, tenant_id=tenant_id, actor_id=human_principal.subject,
        functional_role=access.functional_role,
    )
    timezone_name = connection.execute(
        text("SELECT COALESCE(timezone_name, :f) FROM auditcore.projects WHERE tenant_id=:t"),
        {"t": tenant_id, "f": _FALLBACK_TIMEZONE},
    ).scalar_one_or_none() or _FALLBACK_TIMEZONE
    today = connection.execute(text("SELECT (now() AT TIME ZONE :tz)::date"), {"tz": timezone_name}).scalar_one()
    first, last = from_day or to_day or today, to_day or from_day or today
    if last < first:
        raise HTTPException(status_code=422, detail="'to' is before 'from'.")
    if (last - first).days > 92:
        raise HTTPException(status_code=422, detail="Ask for at most 93 days at a time.")
    _, start, _ = _bounds(connection, tenant_id=tenant_id, day=first)
    _, _, end = _bounds(connection, tenant_id=tenant_id, day=last)
    report = build_failed_pages_report(
        connection, tenant_id=tenant_id, actor_id=human_principal.subject, start=start, end=end,
    )
    return {"from": first.isoformat(), "to": last.isoformat(), "timezone": timezone_name, **report}
