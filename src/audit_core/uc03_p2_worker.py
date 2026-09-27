"""UC03 Phase 2 durable worker.

Run as a separate process:
    python -m audit_core.uc03_p2_worker

The API process never executes document splitting, DI upload/finalize, rule
verification or reconciliation inline. Work is claimed from auditcore.p2_work_queue
with SKIP LOCKED under Tenant RLS and retried durably.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import structlog
from pypdf import PdfReader, PdfWriter
from sqlalchemy import Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import get_engine
from audit_core.uc03_document_capture_v2 import (
    _candidate_type_keys,
    _ensure_di_context,
    _requirement_refs_by_document_type_key,
    _requirements_with_open_slot,
    get_di_capture_v2_client,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.uc03_p2_stage import recompute_booking_stage
from audit_core.uc03_p2_storage import get_p2_document_storage
from audit_core.uc03_unified_document_capture import (
    _merged_candidate_requirements,
    _receipt_defaults_to_delivery,
    _requirements_owned_by_stage,
    reconcile_unified_documents,
)

logger = structlog.get_logger(__name__)

_MAX_ATTEMPTS = int(os.environ.get("P2_WORKER_MAX_ATTEMPTS", "12"))
_POLL_SECONDS = float(os.environ.get("P2_WORKER_POLL_SECONDS", "1.0"))
_RECONCILE_DELAY_SECONDS = int(os.environ.get("P2_RECONCILE_DELAY_SECONDS", "2"))
_MAX_PDF_PAGES = int(os.environ.get("P2_MAX_PDF_PAGES", "100"))
_WORKER_CONCURRENCY = max(1, int(os.environ.get("P2_WORKER_CONCURRENCY", "4")))
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


class RescheduleWork(RuntimeError):
    def __init__(self, message: str, *, delay_seconds: int) -> None:
        super().__init__(message)
        self.delay_seconds = delay_seconds


@dataclass(frozen=True)
class WorkItem:
    tenant_id: str
    work_id: UUID
    journey_id: UUID
    work_type: str
    work_key: str
    payload: dict[str, Any]
    attempt_count: int
    requested_version: int | None
    correlation_id: str | None


def _active_tenants(engine: Engine) -> list[str]:
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        return list(
            connection.execute(
                text(
                    """
                    SELECT DISTINCT tenant_id
                    FROM auditcore.projects
                    WHERE project_status='ACTIVE'
                    ORDER BY tenant_id
                    """
                )
            ).scalars().all()
        )


def _claim_for_tenant(engine: Engine, tenant_id: str, limit: int) -> list[WorkItem]:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(
            text(
                """
                SELECT tenant_id, work_id, journey_id, work_type, work_key,
                       payload, attempt_count, requested_version, correlation_id
                FROM auditcore.p2_work_queue
                WHERE tenant_id=:tenant_id
                  AND work_status IN ('PENDING','RETRY_WAIT')
                  AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc <= now())
                  AND (lease_expires_at_utc IS NULL OR lease_expires_at_utc <= now())
                ORDER BY created_at_utc
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
                """
            ),
            {"tenant_id": tenant_id, "limit": limit},
        ).mappings().all()
        if not rows:
            return []
        ids = [row["work_id"] for row in rows]
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_work_queue
                SET work_status='CLAIMED',
                    attempt_count=attempt_count+1,
                    lease_expires_at_utc=now() + interval '5 minutes',
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND work_id = ANY(:ids)
                """
            ),
            {"tenant_id": tenant_id, "ids": ids},
        )
        return [
            WorkItem(
                tenant_id=str(row["tenant_id"]),
                work_id=UUID(str(row["work_id"])),
                journey_id=UUID(str(row["journey_id"])),
                work_type=str(row["work_type"]),
                work_key=str(row["work_key"]),
                payload=dict(row["payload"] or {}),
                attempt_count=int(row["attempt_count"] or 0) + 1,
                requested_version=(
                    int(row["requested_version"])
                    if row["requested_version"] is not None
                    else None
                ),
                correlation_id=row["correlation_id"],
            )
            for row in rows
        ]


def _complete(engine: Engine, work: WorkItem) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        row = connection.execute(
            text(
                """
                SELECT requested_version
                FROM auditcore.p2_work_queue
                WHERE tenant_id=:tenant_id AND work_id=:work_id
                FOR UPDATE
                """
            ),
            {"tenant_id": work.tenant_id, "work_id": work.work_id},
        ).mappings().one()

        latest_requested = (
            int(row["requested_version"])
            if row["requested_version"] is not None
            else None
        )
        processed = work.requested_version
        stale_after_run = (
            processed is not None
            and latest_requested is not None
            and latest_requested > processed
        )

        connection.execute(
            text(
                """
                UPDATE auditcore.p2_work_queue
                SET work_status=:status,
                    processed_version=COALESCE(:processed_version, processed_version),
                    next_attempt_at_utc=NULL,
                    lease_expires_at_utc=NULL,
                    last_error=NULL,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND work_id=:work_id
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "work_id": work.work_id,
                "status": "PENDING" if stale_after_run else "COMPLETED",
                "processed_version": processed,
            },
        )


def _reschedule(engine: Engine, work: WorkItem, exc: RescheduleWork) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_work_queue
                SET work_status='PENDING',
                    next_attempt_at_utc=now() + (:delay_seconds * interval '1 second'),
                    lease_expires_at_utc=NULL,
                    last_error=:last_error,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND work_id=:work_id
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "work_id": work.work_id,
                "delay_seconds": max(1, exc.delay_seconds),
                "last_error": str(exc)[:1800],
            },
        )
        if work.work_type == "DOCUMENT_RECONCILE":
            try:
                queue_id = UUID(work.work_key)
            except ValueError:
                queue_id = None
            if queue_id is not None:
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.p2_document_queue
                        SET queue_status=CASE
                              WHEN queue_status='RETRY_WAIT' THEN 'CLASSIFYING'
                              ELSE queue_status
                            END,
                            updated_at_utc=now()
                        WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                        """
                    ),
                    {"tenant_id": work.tenant_id, "queue_id": queue_id},
                )


def _fail(engine: Engine, work: WorkItem, exc: Exception) -> None:
    terminal = work.attempt_count >= _MAX_ATTEMPTS
    delay = min(300, max(2, 2 ** min(work.attempt_count, 8)))
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_work_queue
                SET work_status=:status,
                    next_attempt_at_utc=CASE
                      WHEN :terminal THEN NULL
                      ELSE now() + (:delay_seconds * interval '1 second')
                    END,
                    lease_expires_at_utc=NULL,
                    last_error=:last_error,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND work_id=:work_id
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "work_id": work.work_id,
                "status": "DEAD_LETTER" if terminal else "RETRY_WAIT",
                "terminal": terminal,
                "delay_seconds": delay,
                "last_error": f"{exc.__class__.__name__}: {str(exc)[:1800]}",
            },
        )
        if work.work_type in {"DOCUMENT_INGEST", "DOCUMENT_RECONCILE"}:
            queue_status = "DEAD_LETTER" if terminal else "RETRY_WAIT"
            try:
                queue_id = UUID(work.work_key)
            except ValueError:
                queue_id = None
            if queue_id is not None:
                batch_id = connection.execute(
                    text(
                        """
                        UPDATE auditcore.p2_document_queue
                        SET queue_status=:queue_status,
                            last_error=:last_error,
                            updated_at_utc=now()
                        WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                        RETURNING batch_id
                        """
                    ),
                    {
                        "tenant_id": work.tenant_id,
                        "queue_id": queue_id,
                        "queue_status": queue_status,
                        "last_error": f"{exc.__class__.__name__}: {str(exc)[:1800]}",
                    },
                ).scalar_one_or_none()
                if terminal and batch_id is not None:
                    _refresh_batch_status(connection, work.tenant_id, UUID(str(batch_id)))
        elif work.work_type == "SPLIT_BATCH" and terminal:
            try:
                batch_id = UUID(work.work_key)
            except ValueError:
                batch_id = None
            if batch_id is not None:
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.p2_upload_batches
                        SET batch_status='FAILED', updated_at_utc=now()
                        WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                        """
                    ),
                    {"tenant_id": work.tenant_id, "batch_id": batch_id},
                )

        if terminal:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_activity_events (
                        tenant_id, journey_id, event_type, subject_type,
                        subject_id, details, correlation_id
                    ) VALUES (
                        :tenant_id, :journey_id, 'P2_WORK_DEAD_LETTER',
                        'WORK_ITEM', :subject_id, CAST(:details AS jsonb), :correlation_id
                    )
                    """
                ),
                {
                    "tenant_id": work.tenant_id,
                    "journey_id": work.journey_id,
                    "subject_id": str(work.work_id),
                    "details": json.dumps({
                        "workType": work.work_type,
                        "workKey": work.work_key,
                        "attempts": work.attempt_count,
                        "error": str(exc)[:1800],
                    }),
                    "correlation_id": work.correlation_id,
                },
            )


def _enqueue(
    connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    work_type: str,
    work_key: str,
    payload: dict[str, Any],
    correlation_id: str | None,
    delay_seconds: int = 0,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key,
                payload, work_status, next_attempt_at_utc, correlation_id
            ) VALUES (
                :tenant_id, :journey_id, :work_type, :work_key,
                CAST(:payload AS jsonb), 'PENDING',
                CASE WHEN :delay_seconds > 0
                     THEN now() + (:delay_seconds * interval '1 second')
                     ELSE NULL END,
                :correlation_id
            )
            ON CONFLICT (tenant_id, work_type, work_key)
            DO UPDATE SET payload=EXCLUDED.payload,
                          work_status='PENDING',
                          next_attempt_at_utc=EXCLUDED.next_attempt_at_utc,
                          lease_expires_at_utc=NULL,
                          last_error=NULL,
                          updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "work_type": work_type,
            "work_key": work_key,
            "payload": json.dumps(payload, default=str),
            "delay_seconds": delay_seconds,
            "correlation_id": correlation_id,
        },
    )


def _single_page_pdf(page) -> bytes:
    writer = PdfWriter()
    writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _split_batch(engine: Engine, work: WorkItem) -> None:
    storage = get_p2_document_storage()
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        batch = connection.execute(
            text(
                """
                SELECT batch_id, original_filename, content_type, original_object_key,
                       uploaded_by_actor_id, batch_status
                FROM auditcore.p2_upload_batches
                WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                """
            ),
            {"tenant_id": work.tenant_id, "batch_id": UUID(work.work_key)},
        ).mappings().one()
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_upload_batches
                SET batch_status='SPLITTING', updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                """
            ),
            {"tenant_id": work.tenant_id, "batch_id": batch["batch_id"]},
        )

    # All object-storage I/O is deliberately outside a DB transaction. A retry
    # writes the same deterministic page object keys, so an API/worker restart
    # cannot create duplicate queue identities and no DB connection is held idle.
    payload = storage.get_object(str(batch["original_object_key"]))
    digest = hashlib.sha256(payload).hexdigest()
    content_type = str(batch["content_type"] or "")
    filename = str(batch["original_filename"])
    is_pdf = content_type.lower() == "application/pdf" or filename.lower().endswith(".pdf")

    pages: list[tuple[bytes, str]] = []
    if is_pdf:
        reader = PdfReader(io.BytesIO(payload))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ValueError("Encrypted PDF is not supported") from exc
        if not 1 <= len(reader.pages) <= _MAX_PDF_PAGES:
            raise ValueError(f"PDF page count {len(reader.pages)} is outside allowed range")
        pages = [(_single_page_pdf(page), "application/pdf") for page in reader.pages]
    else:
        pages = [(payload, content_type)]

    page_records: list[dict[str, Any]] = []
    for page_number, (page_payload, page_content_type) in enumerate(pages, start=1):
        page_sha = hashlib.sha256(page_payload).hexdigest()
        object_key = (
            f"p2-documents/{work.tenant_id}/{work.journey_id}/"
            f"{batch['batch_id']}/pages/{page_number:04d}-{page_sha[:12]}.pdf"
            if page_content_type == "application/pdf"
            else f"p2-documents/{work.tenant_id}/{work.journey_id}/"
                 f"{batch['batch_id']}/pages/{page_number:04d}-{page_sha[:12]}"
        )
        storage.put_object(object_key, page_payload, content_type=page_content_type)
        page_records.append(
            {
                "pageNumber": page_number,
                "pageSha": page_sha,
                "objectKey": object_key,
                "clientUploadId": (
                    f"p2-{batch['batch_id']}-{page_number}-{page_sha[:12]}"
                ),
            }
        )

    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        for record in page_records:
            queue_id = uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_document_queue (
                        tenant_id, queue_id, batch_id, journey_id, page_number,
                        page_sha256, page_object_key, client_upload_id,
                        queue_status, correlation_id
                    ) VALUES (
                        :tenant_id, :queue_id, :batch_id, :journey_id, :page_number,
                        :page_sha, :object_key, :client_upload_id,
                        'QUEUED', :correlation_id
                    )
                    ON CONFLICT (tenant_id, batch_id, page_number)
                    DO UPDATE SET page_sha256=EXCLUDED.page_sha256,
                                  page_object_key=EXCLUDED.page_object_key,
                                  client_upload_id=EXCLUDED.client_upload_id,
                                  updated_at_utc=now()
                    """
                ),
                {
                    "tenant_id": work.tenant_id,
                    "queue_id": queue_id,
                    "batch_id": batch["batch_id"],
                    "journey_id": work.journey_id,
                    "page_number": record["pageNumber"],
                    "page_sha": record["pageSha"],
                    "object_key": record["objectKey"],
                    "client_upload_id": record["clientUploadId"],
                    "correlation_id": work.correlation_id,
                },
            )
            actual_queue_id = connection.execute(
                text(
                    """
                    SELECT queue_id
                    FROM auditcore.p2_document_queue
                    WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                      AND page_number=:page_number
                    """
                ),
                {
                    "tenant_id": work.tenant_id,
                    "batch_id": batch["batch_id"],
                    "page_number": record["pageNumber"],
                },
            ).scalar_one()
            _enqueue(
                connection,
                tenant_id=work.tenant_id,
                journey_id=work.journey_id,
                work_type="DOCUMENT_INGEST",
                work_key=str(actual_queue_id),
                payload={
                    "queueId": str(actual_queue_id),
                    "batchId": str(batch["batch_id"]),
                    "pageNumber": record["pageNumber"],
                    "uploadedBy": str(batch["uploaded_by_actor_id"]),
                    "uploadedByRole": str(work.payload.get("uploadedByRole") or "PC"),
                },
                correlation_id=work.correlation_id,
            )

        connection.execute(
            text(
                """
                UPDATE auditcore.p2_upload_batches
                SET sha256=:sha256, page_count=:page_count,
                    batch_status='PROCESSING', updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "batch_id": batch["batch_id"],
                "sha256": digest,
                "page_count": len(page_records),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_activity_events (
                    tenant_id, journey_id, event_type, subject_type,
                    subject_id, details, correlation_id
                ) VALUES (
                    :tenant_id, :journey_id, 'UPLOAD_SPLIT_COMPLETE',
                    'UPLOAD_BATCH', :subject_id, CAST(:details AS jsonb), :correlation_id
                )
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "journey_id": work.journey_id,
                "subject_id": str(batch["batch_id"]),
                "details": json.dumps(
                    {"pageCount": len(page_records), "sha256": digest}
                ),
                "correlation_id": work.correlation_id,
            },
        )


def _di_context_and_requirements(engine: Engine, work: WorkItem) -> tuple[str, str, list[str], dict[str, str]]:
    security_client = get_security_oauth_client()
    di_client = get_di_client()
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        booking, delivery = _merged_candidate_requirements(
            connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
        )
        all_requirements = booking + delivery
        receipt_defaults_to_delivery = _receipt_defaults_to_delivery(
            connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
        )
        stage_owned = _requirements_owned_by_stage(
            booking, "BOOKING",
            receipt_defaults_to_delivery=receipt_defaults_to_delivery,
        ) + _requirements_owned_by_stage(
            delivery, "DELIVERY",
            receipt_defaults_to_delivery=receipt_defaults_to_delivery,
        )
        open_requirements = _requirements_with_open_slot(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            requirements=stage_owned,
        )
        context_ref, token = _ensure_di_context(
            connection=connection,
            engine=engine,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            security_client=security_client,
            di_client=di_client,
        )
        return (
            context_ref,
            token,
            _candidate_type_keys(all_requirements),
            _requirement_refs_by_document_type_key(open_requirements),
        )


def _ingest_document(engine: Engine, work: WorkItem) -> None:
    queue_id = UUID(work.work_key)
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        row = connection.execute(
            text(
                """
                SELECT q.queue_id, q.batch_id, q.page_number, q.page_object_key,
                       q.client_upload_id, q.di_document_id, q.queue_status,
                       b.original_filename, b.content_type AS original_content_type,
                       b.uploaded_by_actor_id
                FROM auditcore.p2_document_queue q
                JOIN auditcore.p2_upload_batches b
                  ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
                WHERE q.tenant_id=:tenant_id AND q.queue_id=:queue_id
                """
            ),
            {"tenant_id": work.tenant_id, "queue_id": queue_id},
        ).mappings().one()
        if row["di_document_id"] is not None:
            _enqueue(
                connection,
                tenant_id=work.tenant_id,
                journey_id=work.journey_id,
                work_type="DOCUMENT_RECONCILE",
                work_key=str(queue_id),
                payload=work.payload,
                correlation_id=work.correlation_id,
                delay_seconds=_RECONCILE_DELAY_SECONDS,
            )
            return
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_document_queue
                SET queue_status='DI_UPLOAD_PREPARING', updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                """
            ),
            {"tenant_id": work.tenant_id, "queue_id": queue_id},
        )

    storage = get_p2_document_storage()
    page_payload = storage.get_object(str(row["page_object_key"]))
    context_ref, token, candidates, requirement_refs = _di_context_and_requirements(engine, work)
    v2_client = get_di_capture_v2_client()
    content_type = "application/pdf" if str(row["page_object_key"]).endswith(".pdf") else str(row["original_content_type"] or "application/octet-stream")
    filename = (
        f"{str(row['original_filename']).rsplit('.', 1)[0]}"
        f"-page-{int(row['page_number']):03d}.pdf"
        if content_type == "application/pdf"
        else str(row["original_filename"])
    )

    intent = v2_client.create_upload_intents(
        token=token,
        tenant_id=work.tenant_id,
        external_context_ref=context_ref,
        phase="BOOKING",
        candidate_document_type_keys=candidates,
        requirement_refs_by_document_type_key=requirement_refs,
        files=[{
            "clientUploadId": str(row["client_upload_id"]),
            "filename": filename,
            "contentType": content_type,
            "sizeBytes": len(page_payload),
        }],
    )
    uploads = intent.get("uploads") or []
    if len(uploads) != 1:
        failures = intent.get("failures") or []
        raise RuntimeError(f"DI did not create one upload intent: {failures!r}")
    upload = uploads[0]
    di_document_id = UUID(str(upload["documentId"]))

    with httpx.Client(timeout=45.0) as client:
        response = client.put(
            str(upload["uploadUrl"]),
            headers=dict(upload.get("uploadHeaders") or {}),
            content=page_payload,
        )
        response.raise_for_status()

    v2_client.finalize_document(
        token=token,
        tenant_id=work.tenant_id,
        external_context_ref=context_ref,
        document_id=str(di_document_id),
    )

    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_capture_v2_documents (
                    tenant_id, journey_id, stage_code, di_document_id,
                    client_upload_id, capture_status, original_filename,
                    content_type, created_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', :document_id,
                    :client_upload_id, 'RECEIVING', :filename,
                    :content_type, :actor_id
                )
                ON CONFLICT (tenant_id, journey_id, client_upload_id)
                DO UPDATE SET di_document_id=EXCLUDED.di_document_id,
                              original_filename=EXCLUDED.original_filename,
                              content_type=EXCLUDED.content_type,
                              updated_at_utc=now()
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "journey_id": work.journey_id,
                "document_id": di_document_id,
                "client_upload_id": str(row["client_upload_id"]),
                "filename": filename,
                "content_type": content_type,
                "actor_id": str(row["uploaded_by_actor_id"]),
            },
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_document_queue
                SET di_document_id=:document_id,
                    queue_status='CLASSIFYING',
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "queue_id": queue_id,
                "document_id": di_document_id,
            },
        )
        _enqueue(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            work_type="DOCUMENT_RECONCILE",
            work_key=str(queue_id),
            payload={
                **work.payload,
                "diDocumentId": str(di_document_id),
            },
            correlation_id=work.correlation_id,
            delay_seconds=_RECONCILE_DELAY_SECONDS,
        )


def _reconcile_document(engine: Engine, work: WorkItem) -> None:
    queue_id = UUID(work.work_key)
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        queue = connection.execute(
            text(
                """
                SELECT queue_id, batch_id, di_document_id, queue_status
                FROM auditcore.p2_document_queue
                WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                """
            ),
            {"tenant_id": work.tenant_id, "queue_id": queue_id},
        ).mappings().one()
        if queue["di_document_id"] is None:
            raise RuntimeError("P2 page has no DI document id")
        di_document_id = UUID(str(queue["di_document_id"]))

    context_ref, token, _, _ = _di_context_and_requirements(engine, work)
    v2_client = get_di_capture_v2_client()

    # Reuse the current unified reconciliation so legacy durable evidence and
    # materializers continue to receive the same document/classification semantics.
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        reconcile_unified_documents(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            actor_id=str(work.payload.get("uploadedBy") or "SYSTEM"),
            actor_role=str(work.payload.get("uploadedByRole") or "PC"),
            correlation_id=work.correlation_id or "",
            v2_client=v2_client,
            context_ref=context_ref,
            token=token,
        )

    # Read DI state without keeping a DB transaction open.
    di_item: dict[str, Any] | None = None
    for phase in ("BOOKING", "DELIVERY"):
        payload = v2_client.list_documents(
            token=token,
            tenant_id=work.tenant_id,
            external_context_ref=context_ref,
            phase=phase,
        )
        for item in payload.get("documents") or []:
            if str(item.get("documentId")) == str(di_document_id):
                di_item = item
                break
        if di_item is not None:
            break

    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        extracted_count = int(
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM auditcore.journey_document_extracted_fields
                    WHERE tenant_id=:tenant_id
                      AND journey_id=:journey_id
                      AND di_document_id=:document_id
                    """
                ),
                {
                    "tenant_id": work.tenant_id,
                    "journey_id": work.journey_id,
                    "document_id": di_document_id,
                },
            ).scalar_one()
            or 0
        )
        local = connection.execute(
            text(
                """
                SELECT classified_document_type_key, stage_code
                FROM auditcore.document_capture_v2_documents
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND di_document_id=:document_id
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "journey_id": work.journey_id,
                "document_id": di_document_id,
            },
        ).mappings().one_or_none()

        if extracted_count > 0:
            status = "READY"
        elif di_item is None:
            status = "RETRY_WAIT"
        elif di_item.get("classifiedDocumentTypeKey"):
            status = "EXTRACTING"
        else:
            status = "CLASSIFYING"

        connection.execute(
            text(
                """
                UPDATE auditcore.p2_document_queue
                SET queue_status=:status,
                    classified_document_type=:document_type,
                    business_stage=:business_stage,
                    extracted_field_count=:field_count,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "queue_id": queue_id,
                "status": status,
                "document_type": (
                    local["classified_document_type_key"] if local
                    else (di_item or {}).get("classifiedDocumentTypeKey")
                ),
                "business_stage": local["stage_code"] if local else None,
                "field_count": extracted_count,
            },
        )

        if status == "READY":
            _enqueue(
                connection,
                tenant_id=work.tenant_id,
                journey_id=work.journey_id,
                work_type="STAGE_RECOMPUTE",
                work_key=f"booking:{work.journey_id}",
                payload={"stage": "BOOKING"},
                correlation_id=work.correlation_id,
            )
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_activity_events (
                        tenant_id, journey_id, event_type, subject_type,
                        subject_id, details, correlation_id
                    ) VALUES (
                        :tenant_id, :journey_id, 'DOCUMENT_READY',
                        'DOCUMENT_PAGE', :subject_id, CAST(:details AS jsonb),
                        :correlation_id
                    )
                    """
                ),
                {
                    "tenant_id": work.tenant_id,
                    "journey_id": work.journey_id,
                    "subject_id": str(queue_id),
                    "details": json.dumps({
                        "diDocumentId": str(di_document_id),
                        "documentType": (
                            local["classified_document_type_key"] if local else None
                        ),
                        "fieldCount": extracted_count,
                    }),
                    "correlation_id": work.correlation_id,
                },
            )
            _refresh_batch_status(connection, work.tenant_id, UUID(str(queue["batch_id"])))
        else:
            raise RescheduleWork(
                f"DI document {di_document_id} is still {status}",
                delay_seconds=_RECONCILE_DELAY_SECONDS,
            )


def _refresh_batch_status(connection, tenant_id: str, batch_id: UUID) -> None:
    counts = connection.execute(
        text(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE queue_status='READY') AS ready,
                   COUNT(*) FILTER (WHERE queue_status IN ('FAILED','DEAD_LETTER')) AS failed
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND batch_id=:batch_id
            """
        ),
        {"tenant_id": tenant_id, "batch_id": batch_id},
    ).mappings().one()
    total, ready, failed = int(counts["total"]), int(counts["ready"]), int(counts["failed"])
    if total > 0 and ready == total:
        status = "COMPLETED"
    elif failed > 0 and ready + failed == total:
        status = "PARTIAL_FAILURE" if ready else "FAILED"
    else:
        status = "PROCESSING"
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_upload_batches
            SET batch_status=:status, updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND batch_id=:batch_id
            """
        ),
        {"tenant_id": tenant_id, "batch_id": batch_id, "status": status},
    )


def _stage_recompute(engine: Engine, work: WorkItem) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        result = recompute_booking_stage(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_activity_events (
                    tenant_id, journey_id, event_type, subject_type,
                    subject_id, details, correlation_id
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING_STAGE_RECOMPUTED',
                    'JOURNEY', :subject_id, CAST(:details AS jsonb), :correlation_id
                )
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "journey_id": work.journey_id,
                "subject_id": str(work.journey_id),
                "details": json.dumps(result, default=str),
                "correlation_id": work.correlation_id,
            },
        )


def _task_verify(engine: Engine, work: WorkItem) -> None:
    task_id = UUID(work.work_key)
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        task = connection.execute(
            text(
                """
                SELECT task_id, source_type, source_code, reference, task_status
                FROM auditcore.p2_tasks
                WHERE tenant_id=:tenant_id AND task_id=:task_id
                FOR UPDATE
                """
            ),
            {"tenant_id": work.tenant_id, "task_id": task_id},
        ).mappings().one()
        if task["task_status"] == "VERIFIED_COMPLETE":
            return
        if task["task_status"] != "VERIFYING":
            raise RuntimeError(f"Task is not awaiting machine verification: {task['task_status']}")

        source_code = str(task["source_code"] or "")
        state = connection.execute(
            text(
                """
                SELECT control_status
                FROM auditcore.p2_control_state
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND control_code=:control_code
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "journey_id": work.journey_id,
                "control_code": source_code,
            },
        ).scalar_one_or_none()

        if state == "PASS":
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_tasks
                    SET task_status='VERIFIED_COMPLETE',
                        verified_at_utc=now(), updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND task_id=:task_id
                    """
                ),
                {"tenant_id": work.tenant_id, "task_id": task_id},
            )
            return
        if state == "FAIL":
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_tasks
                    SET task_status='RETURNED', updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND task_id=:task_id
                    """
                ),
                {"tenant_id": work.tenant_id, "task_id": task_id},
            )
            return

        # Fail closed: until a P2 control executor has evaluated the exact
        # originating control, the task remains VERIFYING and work retries.
        if source_code:
            _enqueue(
                connection,
                tenant_id=work.tenant_id,
                journey_id=work.journey_id,
                work_type="CONTROL_EVALUATE",
                work_key=f"{work.journey_id}:{source_code}",
                payload={"controlCode": source_code, "taskId": str(task_id)},
                correlation_id=work.correlation_id,
            )
        raise RescheduleWork(
            f"Originating control {source_code or '<missing>'} has no PASS/FAIL P2 result yet",
            delay_seconds=2,
        )


def _control_evaluate(engine: Engine, work: WorkItem) -> None:
    # P2 is the single control-state authority, but existing native/external
    # engines remain executors. A control is only registered here once its exact
    # execution adapter is validated; unknown controls retry/dead-letter instead
    # of being silently marked PASS.
    control_code = str(work.payload.get("controlCode") or "")
    if not control_code:
        raise ValueError("CONTROL_EVALUATE requires controlCode")
    raise RuntimeError(
        f"P2 control executor is not registered for {control_code}; "
        "verification remains fail-closed"
    )


def process_work(engine: Engine, work: WorkItem) -> None:
    handlers = {
        "SPLIT_BATCH": _split_batch,
        "DOCUMENT_INGEST": _ingest_document,
        "DOCUMENT_RECONCILE": _reconcile_document,
        "STAGE_RECOMPUTE": _stage_recompute,
        "TASK_VERIFY": _task_verify,
        "CONTROL_EVALUATE": _control_evaluate,
    }
    handler = handlers.get(work.work_type)
    if handler is None:
        raise ValueError(f"Unsupported P2 work type {work.work_type}")
    handler(engine, work)


def run_once(engine: Engine | None = None) -> int:
    engine = engine or get_engine()
    claimed: list[WorkItem] = []
    for tenant_id in _active_tenants(engine):
        remaining = _WORKER_CONCURRENCY - len(claimed)
        if remaining <= 0:
            break
        claimed.extend(_claim_for_tenant(engine, tenant_id, remaining))
    if not claimed:
        return 0

    with ThreadPoolExecutor(max_workers=_WORKER_CONCURRENCY) as pool:
        futures = {pool.submit(process_work, engine, item): item for item in claimed}
        for future in as_completed(futures):
            work = futures[future]
            try:
                future.result()
            except RescheduleWork as exc:
                _reschedule(engine, work, exc)
            except Exception as exc:
                logger.warning(
                    "p2_work_failed",
                    tenant_id=work.tenant_id,
                    journey_id=str(work.journey_id),
                    work_type=work.work_type,
                    work_key=work.work_key,
                    exc_info=True,
                )
                _fail(engine, work, exc)
            else:
                _complete(engine, work)
    return len(claimed)


def main() -> None:
    engine = get_engine()
    logger.info(
        "p2_worker_started",
        worker_id=_WORKER_ID,
        concurrency=_WORKER_CONCURRENCY,
    )
    while True:
        processed = run_once(engine)
        if processed == 0:
            time.sleep(_POLL_SECONDS)


if __name__ == "__main__":
    main()
