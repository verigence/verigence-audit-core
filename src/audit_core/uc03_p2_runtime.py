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
        WHERE tenant_id=:tenant_id AND journey_id=:journey_id) AS photos
    """
)


def fact_fingerprint(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str:
    row = connection.execute(
        _FINGERPRINT_SQL, {"tenant_id": tenant_id, "journey_id": journey_id}
    ).mappings().one()
    material = "|".join(str(row[key] or "") for key in ("facts", "payments", "evidence", "legacy_review", "photos"))
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
