"""Shared Phase 2 runtime primitives.

One place for the three things every P2 write path needs:

- ``note_facts_changed``: a P2 code path changed Journey facts (document ready,
  field corrected, document replaced/voided). Bumps the Journey fact version,
  coalesces a STAGE_RECOMPUTE, requeues controls that verification tasks are
  waiting on, and records the new fact fingerprint so the worker's safety
  sweep does not re-trigger for the same change.
- ``fact_fingerprint``: a cheap digest of every fact the stage engine and
  controls read. The worker sweep compares it with the stored fingerprint to
  catch fact changes made outside P2 code paths (for example DI re-delivering
  facts through the existing document-link sync).
- ``record_activity``: the append-only activity feed the Web polls.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


def record_activity(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    event_type: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    details: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_activity_events (
                tenant_id, journey_id, event_type, subject_type,
                subject_id, details, correlation_id
            ) VALUES (
                :tenant_id, :journey_id, :event_type, :subject_type,
                :subject_id, CAST(:details AS jsonb), :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "event_type": event_type,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "details": json.dumps(details or {}, default=str),
            "correlation_id": correlation_id,
        },
    )


_FINGERPRINT_SQL = text(
    """
    SELECT
      (SELECT concat_ws(':', COUNT(*), MAX(updated_at_utc),
                        SUM(hashtext(COALESCE(effective_value::text, ''))))
         FROM auditcore.journey_document_extracted_fields
        WHERE tenant_id=:tenant_id AND journey_id=:journey_id) AS facts,
      (SELECT concat_ws(':', COUNT(*), MAX(updated_at_utc), SUM(amount))
         FROM auditcore.payments
        WHERE tenant_id=:tenant_id AND journey_id=:journey_id) AS payments,
      (SELECT concat_ws(':', COUNT(*),
                        string_agg(evidence_id::text || association_status, ','
                                   ORDER BY evidence_id))
         FROM auditcore.evidence
        WHERE tenant_id=:tenant_id AND journey_id=:journey_id) AS evidence,
      (SELECT concat_ws(':', COUNT(*), MAX(updated_at_utc))
         FROM auditcore.workflow_tasks
        WHERE tenant_id=:tenant_id AND journey_id=:journey_id
          AND task_type='MANUAL_VERIFICATION_REVIEW') AS legacy_review,
      (SELECT concat_ws(':', COUNT(*) FILTER (WHERE deleted_at_utc IS NULL),
                        MAX(COALESCE(deleted_at_utc, uploaded_at_utc)))
         FROM auditcore.delivery_vehicle_photos
        WHERE tenant_id=:tenant_id AND journey_id=:journey_id) AS photos,
      (SELECT concat_ws(':', MAX(jp.updated_at_utc), MAX(jp.product_sku_id::text),
                        (SELECT MAX(updated_at_utc) FROM auditcore.finance_records f
                          WHERE f.tenant_id=:tenant_id AND f.journey_id=:journey_id),
                        (SELECT pricing_effective_on FROM auditcore.bookings b
                          WHERE b.tenant_id=:tenant_id AND b.journey_id=:journey_id),
                        (SELECT COUNT(*) FROM auditcore.p2_vehicle_identifications vi
                          WHERE vi.tenant_id=:tenant_id AND vi.journey_id=:journey_id))
         FROM auditcore.journey_products jp
        WHERE jp.tenant_id=:tenant_id AND jp.journey_id=:journey_id) AS deal
    """
)


def fact_fingerprint(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str:
    row = connection.execute(
        _FINGERPRINT_SQL, {"tenant_id": tenant_id, "journey_id": journey_id}
    ).mappings().one()
    material = "|".join(str(row[key] or "") for key in ("facts", "payments", "evidence", "legacy_review", "photos", "deal"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def note_facts_changed(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    reason: str,
    correlation_id: str | None = None,
) -> int:
    """Durably request downstream recomputation for a Journey fact change.

    Runs inside the caller's transaction, so the request commits atomically
    with the fact change itself (or not at all)."""
    version = int(
        connection.execute(
            text("SELECT auditcore.p2_request_stage_recompute(:tenant_id, :journey_id)"),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_journey_runtime
            SET fact_fingerprint=:fingerprint
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "fingerprint": fact_fingerprint(connection, tenant_id=tenant_id, journey_id=journey_id),
        },
    )
    record_activity(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        event_type="FACTS_CHANGED",
        subject_type="JOURNEY",
        subject_id=str(journey_id),
        details={"reason": reason, "factVersion": version},
        correlation_id=correlation_id,
    )
    return version


def enqueue_work(
    connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    work_type: str,
    work_key: str,
    payload: dict[str, Any],
    correlation_id: str | None,
    delay_seconds: int = 0,
    requested_version: int | None = None,
) -> None:
    """Idempotently request work. Re-requesting an item always marks it dirty
    (requested_version advances), so an item that is running when a new
    request arrives runs again after it completes."""
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key,
                payload, requested_version, work_status,
                next_attempt_at_utc, correlation_id
            ) VALUES (
                :tenant_id, :journey_id, :work_type, :work_key,
                CAST(:payload AS jsonb), COALESCE(:requested_version, 1), 'PENDING',
                CASE WHEN :delay_seconds > 0
                     THEN now() + (:delay_seconds * interval '1 second')
                     ELSE NULL END,
                :correlation_id
            )
            ON CONFLICT (tenant_id, work_type, work_key)
            DO UPDATE SET payload=EXCLUDED.payload,
                          requested_version=GREATEST(
                            COALESCE(auditcore.p2_work_queue.requested_version, 0) + 1,
                            COALESCE(:requested_version, 0)
                          ),
                          work_status=CASE
                            WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                              THEN auditcore.p2_work_queue.work_status
                            ELSE 'PENDING'
                          END,
                          attempt_count=CASE
                            WHEN auditcore.p2_work_queue.work_status IN ('COMPLETED','DEAD_LETTER','CANCELLED')
                              THEN 0
                            ELSE auditcore.p2_work_queue.attempt_count
                          END,
                          next_attempt_at_utc=CASE
                            WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                              THEN auditcore.p2_work_queue.next_attempt_at_utc
                            WHEN auditcore.p2_work_queue.work_status IN ('PENDING','RETRY_WAIT')
                              AND auditcore.p2_work_queue.next_attempt_at_utc IS NOT NULL
                              AND EXCLUDED.next_attempt_at_utc IS NOT NULL
                              THEN LEAST(auditcore.p2_work_queue.next_attempt_at_utc,
                                         EXCLUDED.next_attempt_at_utc)
                            ELSE EXCLUDED.next_attempt_at_utc
                          END,
                          last_error=CASE
                            WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                              THEN auditcore.p2_work_queue.last_error
                            ELSE NULL
                          END,
                          updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "work_type": work_type,
            "work_key": work_key,
            "payload": json.dumps(payload, default=str),
            "requested_version": requested_version,
            "delay_seconds": delay_seconds,
            "correlation_id": correlation_id,
        },
    )


def request_page_recovery(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    queue_id: UUID,
    marker: str,
    requested_by: str,
    correlation_id: str | None,
) -> str:
    """Ask the worker to recover a FAILED page: it looks at what Document
    Intelligence already holds for the page first and uploads it again only
    if DI gave it up (uc03_p2_worker._recover_page). A page DI never
    received is sent straight away. ``marker`` is ``r`` for a PC's Retry,
    ``n`` for the nightly sweep. Returns the client upload id in force."""
    row = connection.execute(
        text(
            """
            SELECT di_document_id, client_upload_id FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
            """
        ),
        {"tenant_id": tenant_id, "queue_id": queue_id},
    ).mappings().one()
    if row["di_document_id"] is None:
        return requeue_page_for_ingest(
            connection, tenant_id=tenant_id, journey_id=journey_id, queue_id=queue_id,
            client_upload_id=str(row["client_upload_id"]), marker=marker, requested_by=requested_by,
            correlation_id=correlation_id,
        )
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status='QUEUED', status_reason=NULL, last_error=NULL, updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
            """
        ),
        {"tenant_id": tenant_id, "queue_id": queue_id},
    )
    enqueue_work(
        connection, tenant_id=tenant_id, journey_id=journey_id, work_type="DOCUMENT_INGEST",
        work_key=str(queue_id), payload={"queueId": str(queue_id), "uploadedBy": requested_by, "recover": marker},
        correlation_id=correlation_id,
    )
    return str(row["client_upload_id"])


def requeue_page_for_ingest(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    queue_id: UUID,
    client_upload_id: str,
    marker: str,
    requested_by: str,
    correlation_id: str | None,
) -> str:
    """Send a page to Document Intelligence again as a fresh document: the
    queue row is reset and its ingest work re-queued. ``marker`` tells the
    attempts apart in the client upload id: ``r`` for a PC's Retry, ``n``
    for the nightly sweep (which re-drives a page once). Returns the new
    client upload id."""
    base = client_upload_id.split("~", 1)[0]
    attempt = connection.execute(
        text("SELECT COUNT(*) FROM auditcore.p2_document_queue WHERE tenant_id=:t AND client_upload_id LIKE :p"),
        {"t": tenant_id, "p": f"{base}~{marker}%"},
    ).scalar_one()
    new_id = f"{base}~{marker}{int(attempt) + 1}"
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status='QUEUED', client_upload_id=:client_upload_id, di_document_id=NULL,
                di_state=NULL, di_processing_status=NULL, di_submitted_at_utc=NULL,
                di_processed_seen_at_utc=NULL, status_reason=NULL, last_error=NULL,
                attempt_count=0, extracted_field_count=0, updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
            """
        ),
        {"tenant_id": tenant_id, "queue_id": queue_id, "client_upload_id": new_id},
    )
    enqueue_work(
        connection, tenant_id=tenant_id, journey_id=journey_id, work_type="DOCUMENT_INGEST",
        work_key=str(queue_id), payload={"queueId": str(queue_id), "uploadedBy": requested_by},
        correlation_id=correlation_id,
    )
    return new_id
