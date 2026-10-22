"""Taking a failed page off the Journey: by a person, and by the day-end sweep.

A person (PC or TL) removes a refused or failed page with
``POST /pages/{queue}:remove`` (2026-10-21, #480). Product owner, 2026-10-22:
every day at 02:00 IST (20:30 UTC, P2_DAYEND_SWEEP_UTC) the worker does the
same for every page that ended FAILED after its retries -- pages refused by
the scan-quality check included -- instead of expecting the PC to remove it.

Both go through ``take_page_off_journey``: any Document Intelligence document
held for the page is voided the way deleting a document voids it, the page
unit is cancelled with any work still queued for it, the file's status is
refreshed and the removal is recorded. Neither touches the stored page file
(the object in storage): the failure report and the PC's "Document to
resubmit" task open it by a presigned link.

The two differ in what they leave behind:

- a person's removal overwrites the page's reason, and the file's tasks close
  naming the removal (``sync_processing_failure_tasks(removal=...)``);
- the sweep keeps the page's failure code and reason and records PAGE_SWEPT.
  The page then counts as a failed page of its file until it is resubmitted
  (``swept_pending_sql``), so the file's HIGH "Document to resubmit" task
  stays open, as the product owner decided, and the failure report keeps
  listing it. A re-upload of the page, or a person's removal of the swept
  page, ends that.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.uc03_p2_runtime import PAGE_SWEPT_EVENT, record_activity

logger = structlog.get_logger(__name__)

# The sweep's own name in the activity feed and in who/what columns.
SWEEP_ACTOR = "day-end sweep"
SWEEP_RUN_NAME = "P2_DAYEND_SWEEP"
# A page with no failure code (none is expected) is swept with this one, so it
# stays recognisable as a swept page that still waits to be resubmitted.
UNKNOWN_FAILURE_CODE = "PAGE_FAILED"

# "Not completed or cancelled": the same two tests the nightly review and the
# nightly SKU retry use (review not completed; the Booking not cancelled, a
# duplicate, or closed with no delivery).
_OPEN_JOURNEY = """
    j.review_completed_at_utc IS NULL
    AND NOT EXISTS (
      SELECT 1 FROM auditcore.journey_stage_states s
      WHERE s.tenant_id=j.tenant_id AND s.journey_id=j.journey_id AND s.stage_code='BOOKING'
        AND (s.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
             OR s.closure_disposition='NO_DELIVERY'))
"""


def take_page_off_journey(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    unit: dict[str, Any],
    actor_id: str,
    activity_type: str,
    activity_details: dict[str, Any],
    status_reason: str | None,
    ensure_code: str | None = None,
    correlation_id: str | None = None,
) -> bool:
    """Cancel one failed page unit and take it off the Journey (the body of
    ``:remove``, shared with the sweep). Returns whether a Document
    Intelligence document held for the page was voided.

    ``status_reason`` replaces the page's reason (None keeps it);
    ``ensure_code`` is stored as the failure code when the page has none.
    Nothing here touches the stored page file."""
    from audit_core.uc03_p2_documents import void_p2_document
    from audit_core.uc03_p2_worker import _refresh_batch_status

    queue_id = UUID(str(unit["queue_id"]))
    voided = False
    if unit.get("di_document_id") is not None:
        held = connection.execute(
            text(
                """
                SELECT 1 FROM auditcore.document_capture_v2_documents
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND capture_status <> 'SUPERSEDED'
                UNION ALL
                SELECT 1 FROM auditcore.evidence
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND association_status='ACTIVE'
                LIMIT 1
                """
            ),
            {"t": tenant_id, "j": journey_id, "d": unit["di_document_id"]},
        ).scalar_one_or_none()
        if held:
            void_p2_document(connection, tenant_id=tenant_id, journey_id=journey_id,
                             document_id=UUID(str(unit["di_document_id"])), actor_id=actor_id,
                             activity_details={"via": activity_type, "queueId": str(queue_id)})
            voided = True
    # The page unit itself, whether or not a document was held for it. The
    # failure code stays in last_error (reports count refusals by it).
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status='CANCELLED',
                status_reason=CASE WHEN CAST(:replace AS boolean) THEN :reason ELSE status_reason END,
                last_error=COALESCE(last_error, CAST(:ensure_code AS text)),
                updated_at_utc=now()
            WHERE tenant_id=:t AND queue_id=:q
            """
        ),
        {"t": tenant_id, "q": queue_id, "replace": status_reason is not None, "reason": status_reason,
         "ensure_code": ensure_code},
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_work_queue SET work_status='CANCELLED', updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND work_key=:k
              AND work_type IN ('DOCUMENT_INGEST','DOCUMENT_RECONCILE')
              AND work_status NOT IN ('COMPLETED','DEAD_LETTER','CANCELLED')
            """
        ),
        {"t": tenant_id, "j": journey_id, "k": str(queue_id)},
    )
    # The event comes before the file's status is refreshed: a swept page
    # counts as failed through it (swept_pending_sql).
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type=activity_type,
        subject_type="DOCUMENT_PAGE", subject_id=str(queue_id),
        details={**activity_details, "documentVoided": voided}, correlation_id=correlation_id,
    )
    _refresh_batch_status(connection, tenant_id, UUID(str(unit["batch_id"])))
    return voided


def sweep_failed_pages(connection: Connection, *, tenant_id: str, now: datetime | None = None) -> dict[str, int]:
    """The day-end sweep (product owner 2026-10-22): clear from the Journey
    every page that ended FAILED after its retries.

    Selects the page units whose queue_status is FAILED (or DEAD_LETTER, as
    ``:remove`` and the failure report treat it) and that are still on the
    Journey, in Journeys not completed or cancelled. That includes the pages
    the scan-quality check refused (DI_QUALITY_*, product owner 2026-10-22):
    the sweep is only a clean-up, so their image-quality task stays open
    exactly as the resubmit task does. READY, NEEDS_REVIEW, SUPPORTING and
    pages still being worked on are never touched, nor is any other state.

    Each page is taken off the Journey through ``take_page_off_journey``
    (cancelled, its Document Intelligence document voided if one is held, its
    stored file kept), keeping its failure code and reason, and a PAGE_SWEPT
    event is recorded. The file's tasks are then refreshed: the HIGH
    "Document to resubmit" task stays open for the swept pages. Idempotent: a
    second run finds nothing to sweep. A Journey that fails is rolled back on
    its own (its pages stay FAILED and are swept by the next day's run); the
    others are not affected. Returns what was done."""
    from audit_core.uc03_p2_task_producer import sync_processing_failure_tasks

    now = now or datetime.now(UTC)
    units = connection.execute(
        text(
            f"""
            SELECT q.queue_id, q.journey_id, q.batch_id, q.page_number, q.page_numbers, q.unit_kind,
                   q.di_document_id, q.last_error, q.status_reason, b.original_filename
            FROM auditcore.p2_document_queue q
            JOIN auditcore.journeys j ON j.tenant_id=q.tenant_id AND j.journey_id=q.journey_id
            JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
            WHERE q.tenant_id=:t AND q.queue_status IN ('FAILED','DEAD_LETTER')
              AND {_OPEN_JOURNEY}
            ORDER BY q.journey_id, q.page_number, q.queue_id
            FOR UPDATE OF q SKIP LOCKED
            """
        ),
        {"t": tenant_id},
    ).mappings().all()
    by_journey: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for row in units:
        by_journey[UUID(str(row["journey_id"]))].append(dict(row))
    swept = 0
    voided = 0
    failed_journeys: list[str] = []
    for journey_id, pages in by_journey.items():
        try:
            with connection.begin_nested():
                for page in pages:
                    numbers = [int(n) for n in (page["page_numbers"] or [page["page_number"]])]
                    if take_page_off_journey(
                        connection, tenant_id=tenant_id, journey_id=journey_id, unit=page, actor_id=SWEEP_ACTOR,
                        activity_type=PAGE_SWEPT_EVENT, status_reason=None, ensure_code=UNKNOWN_FAILURE_CODE,
                        activity_details={
                            "queueId": str(page["queue_id"]), "batchId": str(page["batch_id"]), "pages": numbers,
                            "filename": page["original_filename"],
                            "code": page["last_error"] or UNKNOWN_FAILURE_CODE, "reason": page["status_reason"],
                            "by": SWEEP_ACTOR, "sweptAtUtc": now.isoformat(),
                        },
                    ):
                        voided += 1
                sync_processing_failure_tasks(connection, tenant_id=tenant_id, journey_id=journey_id)
            swept += len(pages)
        except Exception:
            # One Journey's failure must not stop the others: it is rolled back
            # (its pages stay FAILED), logged, and swept by the next run.
            failed_journeys.append(str(journey_id))
            logger.exception("p2_dayend_sweep_journey_failed", tenant_id=tenant_id, journey_id=str(journey_id))
    return {"pages": swept, "journeys": len(by_journey) - len(failed_journeys), "documentsVoided": voided,
            "failedJourneys": len(failed_journeys)}
