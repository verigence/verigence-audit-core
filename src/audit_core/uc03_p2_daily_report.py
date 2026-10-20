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
- tasks: ``p2_tasks`` created on the day, plus REOPENED rounds.

Day boundaries are the Project's own timezone. Known gaps, stated in the
response rather than guessed: the actor of a journey Re-sync is not stored,
and DI's own nightly reprocessing keeps its per-document results in DI.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2

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


def _empty_counts() -> dict[str, Any]:
    return {
        "files": 0, "pages": 0, "duplicateFiles": 0,
        "documents": {"extracted": 0, "readNoValues": 0, "notRead": 0, "waiting": 0, "failed": 0, "rejectedScans": 0},
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

    units = connection.execute(
        text(
            """
            SELECT q.batch_id, q.queue_status, q.last_error
            FROM auditcore.p2_document_queue q
            WHERE q.tenant_id=:t AND q.batch_id = ANY(:batches)
              AND q.queue_status NOT IN ('MERGED','CANCELLED')
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
                    if error.startswith("DI_QUALITY_") or error in _REJECTED_ERRORS:
                        docs["rejectedScans"] += 1
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
        })
        summary["journeys"] += 1
        _add(summary, {k: v for k, v in row.items() if k in _empty_counts()})
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

    return {
        "day": day.isoformat(),
        "timezone": timezone_name,
        "pcs": sorted(pcs.values(), key=lambda s: (s["pcName"] or s["pcActorId"] or "~")),
        "journeys": journeys,
        "notes": [
            "Documents are the units the uploads became after splitting and grouping, in their state now.",
            "Finished is the last document settling; it stays blank while any document of that day's uploads is in flight.",
            "Re-sync counts the journey's 'Re-sync with Document Intelligence'; who pressed it is not stored.",
            "Night jobs run at about 02:00 local time; night-fixed counts pages the sweep asked for that were read afterwards.",
            "Document Intelligence's own nightly re-processing keeps its per-document results in DI and is not counted here.",
        ],
    }


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
