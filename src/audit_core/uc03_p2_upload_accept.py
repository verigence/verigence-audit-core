"""Accepting an uploaded file: the one step that starts its processing.

A PC's upload is prepared by ``uploads:init`` (or ``documents/{id}/replace``):
a ``p2_upload_batches`` row in AWAITING_UPLOAD and a presigned PUT to R2. Once
the file is stored whole, the batch is accepted: AWAITING_UPLOAD -> UPLOADED,
SPLIT_BATCH work queued and an UPLOAD_ACCEPTED event recorded, all in one
small transaction. Two callers, one rule:

- ``:finalize`` (uc03_p2_api.finalize_upload), when the browser confirms the
  upload;
- the worker (uc03_p2_worker.accept_stored_uploads), when the browser stored
  the file but never confirmed it (live, 2 Nov 2026: 3 of the 13 files failed
  as "did not finish uploading within 30 minutes" were in R2 at their full
  size). Product owner: a file saved in R2 must start the usual process.

Both look the object up in storage first (HEAD, with no transaction open) and
call ``accept_upload`` only when it is there at the announced size. The
conditional update makes the acceptance idempotent: exactly one caller wins
the AWAITING_UPLOAD transition, every other one is told the current status.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_p2_runtime import record_activity


def accept_upload(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    batch_id: UUID,
    filename: str,
    size_bytes: int,
    uploaded_by: str,
    uploaded_by_role: str,
    correlation_id: str | None,
    details: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Accept an upload batch whose file is stored whole.

    Runs inside the caller's transaction (tenant context already set); the
    caller has already checked the stored object and holds no storage call
    open. Returns ``(True, "UPLOADED")`` when this call accepted the batch,
    else ``(False, <the batch's current status>)`` and writes nothing.
    ``details`` are added to the UPLOAD_ACCEPTED event's filename and size."""
    accepted = connection.execute(
        text(
            """
            UPDATE auditcore.p2_upload_batches
            SET batch_status='UPLOADED', updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND batch_id=:batch_id
              AND batch_status='AWAITING_UPLOAD'
            RETURNING batch_id
            """
        ),
        {"tenant_id": tenant_id, "batch_id": batch_id},
    ).scalar_one_or_none()
    if accepted is None:
        current = connection.execute(
            text(
                "SELECT batch_status FROM auditcore.p2_upload_batches "
                "WHERE tenant_id=:tenant_id AND batch_id=:batch_id"
            ),
            {"tenant_id": tenant_id, "batch_id": batch_id},
        ).scalar_one()
        return False, str(current)
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key,
                payload, work_status, correlation_id, requested_version
            ) VALUES (
                :tenant_id, :journey_id, 'SPLIT_BATCH', :work_key,
                CAST(:payload AS jsonb), 'PENDING', :correlation_id, 1
            )
            ON CONFLICT (tenant_id, work_type, work_key) DO NOTHING
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "work_key": str(batch_id),
            "payload": json.dumps(
                {
                    "batchId": str(batch_id),
                    "uploadedBy": uploaded_by,
                    "uploadedByRole": uploaded_by_role,
                }
            ),
            "correlation_id": correlation_id,
        },
    )
    record_activity(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        event_type="UPLOAD_ACCEPTED",
        subject_type="UPLOAD_BATCH",
        subject_id=str(batch_id),
        details={"filename": filename, "sizeBytes": size_bytes, **(details or {})},
        correlation_id=correlation_id,
    )
    return True, "UPLOADED"
