"""UC03 Phase 2 durable worker.

Run as a separate process:
    python -m audit_core.uc03_p2_worker

The API process never executes document splitting, DI upload/finalize, rule
verification or reconciliation inline. Work is claimed from auditcore.p2_work_queue
with SKIP LOCKED under Tenant RLS and retried durably.

Queue contract:
- Every claim writes a fresh lease_token. Completion, failure and reschedule
  only apply while the caller still owns that lease, so a worker whose lease
  expired can never overwrite the outcome of the worker that reclaimed it.
- attempt_count counts failures only (including a reclaimed expired lease,
  which means the previous worker died mid-item). Polling reschedules are not
  failures; long waits are bounded by per-page deadlines instead.
- requested_version is a monotonically increasing "dirty" counter. Re-enqueueing
  an item that is currently being processed bumps it, and completion requeues
  the item when a newer request arrived while it ran, so no request is lost.
- No DB transaction is held open across DI, Security or object-storage calls.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import structlog
from pypdf import PdfReader, PdfWriter
from sqlalchemy import Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import get_engine
from audit_core.uc03_document_capture_v2 import (
    _DI_AUDIENCE,
    _candidate_type_keys,
    _ensure_di_context,
    _requirement_refs_by_document_type_key,
    _requirements_with_open_slot,
    get_di_capture_v2_client,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.uc03_p2_controls import (
    evaluate_unit,
    mark_unit_controls,
    request_control_evaluation,
)
from audit_core.uc03_p2_grouping import PageFact, merge_pdf_pages, plan_documents
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_runtime import (
    enqueue_work,
    fact_fingerprint,
    note_facts_changed,
    record_activity,
)
from audit_core.uc03_p2_stage import recompute_journey_stage
from audit_core.uc03_p2_storage import get_p2_document_storage
from audit_core.uc03_p2_task_producer import (
    apply_control_transitions,
    sync_field_review_tasks,
    sync_vehicle_photo_task,
)
from audit_core.uc03_unified_document_capture import (
    _merged_candidate_requirements,
    _receipt_defaults_to_delivery,
    _requirements_owned_by_stage,
    apply_di_classification,
)

logger = structlog.get_logger(__name__)

_MAX_ATTEMPTS = int(os.environ.get("P2_WORKER_MAX_ATTEMPTS", "8"))
_POLL_SECONDS = float(os.environ.get("P2_WORKER_POLL_SECONDS", "1.0"))
_MAX_PDF_PAGES = int(os.environ.get("P2_MAX_PDF_PAGES", "100"))
_WORKER_CONCURRENCY = max(1, int(os.environ.get("P2_WORKER_CONCURRENCY", "6")))
_PER_JOURNEY_CONCURRENCY = max(1, int(os.environ.get("P2_PER_JOURNEY_CONCURRENCY", "3")))
_LEASE_SECONDS = int(os.environ.get("P2_WORKER_LEASE_SECONDS", "600"))
# A page that DI has not settled within this window is failed visibly (with a
# Retry action in the UI) instead of being polled forever.
_PAGE_DEADLINE_SECONDS = int(os.environ.get("P2_PAGE_DEADLINE_SECONDS", str(30 * 60)))
# DI reports PROCESSED before the document-link sync has copied facts into
# Audit Core. Allow the sync this long before treating "no facts" as a result.
_SYNC_GRACE_SECONDS = int(os.environ.get("P2_SYNC_GRACE_SECONDS", "180"))
_FACT_SWEEP_SECONDS = float(os.environ.get("P2_FACT_SWEEP_SECONDS", "30"))
_FACT_SWEEP_WINDOW_MINUTES = int(os.environ.get("P2_FACT_SWEEP_WINDOW_MINUTES", "10"))
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# Page states that still need DI/Audit Core progress.
_PAGE_ACTIVE_STATES = (
    "QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING",
    "DI_FINALIZING", "CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE",
    "RETRY_WAIT",
)
_PAGE_RECONCILE_STATES = ("CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE", "RETRY_WAIT")
_PAGE_SETTLED_STATES = ("READY", "SUPPORTING", "NEEDS_REVIEW", "FAILED", "DEAD_LETTER", "CANCELLED")


class RescheduleWork(RuntimeError):
    def __init__(self, message: str, *, delay_seconds: int) -> None:
        super().__init__(message)
        self.delay_seconds = delay_seconds


class LeaseLost(RuntimeError):
    """The item was reclaimed by another worker; drop this result."""


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
    lease_token: UUID


def _active_tenants(engine: Engine) -> list[str]:
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        tenants = list(
            connection.execute(
                text(
                    """
                    SELECT DISTINCT tenant_id
                    FROM auditcore.projects
                    WHERE project_status='ACTIVE'
                    """
                )
            ).scalars().all()
        )
    # Fairness: a busy tenant must not starve the others of worker slots.
    random.shuffle(tenants)
    return tenants


_CLAIM_SQL = text(
    """
    WITH inflight AS (
        SELECT journey_id, COUNT(*) AS running
        FROM auditcore.p2_work_queue
        WHERE tenant_id=:tenant_id
          AND work_status IN ('CLAIMED','PROCESSING')
          AND lease_expires_at_utc > now()
        GROUP BY journey_id
    ),
    ready AS (
        SELECT work_id, journey_id, created_at_utc,
               row_number() OVER (PARTITION BY journey_id ORDER BY created_at_utc) AS journey_rank
        FROM auditcore.p2_work_queue
        WHERE tenant_id=:tenant_id
          AND (
            work_status IN ('PENDING','RETRY_WAIT')
            OR (work_status IN ('CLAIMED','PROCESSING') AND lease_expires_at_utc <= now())
          )
          AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc <= now())
    ),
    eligible AS (
        SELECT r.work_id
        FROM ready r
        LEFT JOIN inflight i ON i.journey_id=r.journey_id
        WHERE r.journey_rank + COALESCE(i.running, 0) <= :per_journey
        ORDER BY r.created_at_utc
        LIMIT :limit
    )
    SELECT w.tenant_id, w.work_id, w.journey_id, w.work_type, w.work_key,
           w.payload, w.attempt_count, w.requested_version, w.correlation_id,
           (w.work_status IN ('CLAIMED','PROCESSING')) AS reclaimed
    FROM auditcore.p2_work_queue w
    JOIN eligible e ON e.work_id=w.work_id
    WHERE w.tenant_id=:tenant_id
    ORDER BY w.created_at_utc
    FOR UPDATE OF w SKIP LOCKED
    """
)


def _claim_for_tenant(engine: Engine, tenant_id: str, limit: int) -> list[WorkItem]:
    claimed: list[WorkItem] = []
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(
            _CLAIM_SQL,
            {"tenant_id": tenant_id, "limit": limit, "per_journey": _PER_JOURNEY_CONCURRENCY},
        ).mappings().all()
        for row in rows:
            token = uuid4()
            # A reclaimed expired lease means the previous worker died while
            # holding the item: that counts as a failed attempt, so a crash-
            # looping item eventually dead-letters instead of cycling forever.
            attempts = int(row["attempt_count"] or 0) + (1 if row["reclaimed"] else 0)
            if row["reclaimed"] and attempts >= _MAX_ATTEMPTS:
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.p2_work_queue
                        SET work_status='DEAD_LETTER', attempt_count=:attempts,
                            lease_token=NULL, lease_expires_at_utc=NULL,
                            last_error='Worker lease expired repeatedly while processing.',
                            updated_at_utc=now()
                        WHERE tenant_id=:tenant_id AND work_id=:work_id
                        """
                    ),
                    {"tenant_id": tenant_id, "work_id": row["work_id"], "attempts": attempts},
                )
                continue
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_work_queue
                    SET work_status='CLAIMED',
                        attempt_count=:attempts,
                        claim_count=claim_count+1,
                        lease_token=:token,
                        lease_expires_at_utc=now() + (:lease_seconds * interval '1 second'),
                        updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND work_id=:work_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "work_id": row["work_id"],
                    "attempts": attempts,
                    "token": token,
                    "lease_seconds": _LEASE_SECONDS,
                },
            )
            claimed.append(
                WorkItem(
                    tenant_id=str(row["tenant_id"]),
                    work_id=UUID(str(row["work_id"])),
                    journey_id=UUID(str(row["journey_id"])),
                    work_type=str(row["work_type"]),
                    work_key=str(row["work_key"]),
                    payload=dict(row["payload"] or {}),
                    attempt_count=attempts,
                    requested_version=(
                        int(row["requested_version"])
                        if row["requested_version"] is not None
                        else None
                    ),
                    correlation_id=row["correlation_id"],
                    lease_token=token,
                )
            )
    return claimed


def _owned(connection, work: WorkItem, *, lock: bool = True) -> dict[str, Any]:
    """Return the queue row if this worker still owns the lease, else raise."""
    row = connection.execute(
        text(
            f"""
            SELECT requested_version, lease_token
            FROM auditcore.p2_work_queue
            WHERE tenant_id=:tenant_id AND work_id=:work_id
            {"FOR UPDATE" if lock else ""}
            """
        ),
        {"tenant_id": work.tenant_id, "work_id": work.work_id},
    ).mappings().one_or_none()
    if row is None or row["lease_token"] != work.lease_token:
        raise LeaseLost(f"Lease lost for work item {work.work_id}")
    return dict(row)


def _complete(engine: Engine, work: WorkItem) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        row = _owned(connection, work)
        latest_requested = (
            int(row["requested_version"]) if row["requested_version"] is not None else None
        )
        processed = work.requested_version
        stale_after_run = (
            latest_requested is not None
            and (processed is None or latest_requested > processed)
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_work_queue
                SET work_status=CAST(:status AS varchar),
                    processed_version=COALESCE(CAST(:processed_version AS bigint), processed_version),
                    attempt_count=CASE WHEN CAST(:status AS varchar)='COMPLETED' THEN 0 ELSE attempt_count END,
                    next_attempt_at_utc=NULL,
                    lease_token=NULL,
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
        row = _owned(connection, work)
        newer_request = (
            row["requested_version"] is not None
            and (work.requested_version is None or int(row["requested_version"]) > work.requested_version)
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_work_queue
                SET work_status='PENDING',
                    next_attempt_at_utc=CASE
                      WHEN :immediate THEN NULL
                      ELSE now() + (:delay_seconds * interval '1 second')
                    END,
                    lease_token=NULL,
                    lease_expires_at_utc=NULL,
                    last_error=:last_error,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND work_id=:work_id
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "work_id": work.work_id,
                "immediate": newer_request,
                "delay_seconds": max(1, exc.delay_seconds),
                "last_error": str(exc)[:1800],
            },
        )


def _fail(engine: Engine, work: WorkItem, exc: Exception) -> None:
    attempts = work.attempt_count + 1
    terminal = attempts >= _MAX_ATTEMPTS
    delay = min(300, 2 ** min(attempts, 8))
    error = f"{exc.__class__.__name__}: {str(exc)[:1800]}"
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        _owned(connection, work)
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_work_queue
                SET work_status=:status,
                    attempt_count=:attempts,
                    next_attempt_at_utc=CASE
                      WHEN :terminal THEN NULL
                      ELSE now() + (:delay_seconds * interval '1 second')
                    END,
                    lease_token=NULL,
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
                "attempts": attempts,
                "terminal": terminal,
                "delay_seconds": delay,
                "last_error": error,
            },
        )
        if not terminal:
            return

        if work.work_type == "DOCUMENT_INGEST":
            _settle_page(
                connection,
                tenant_id=work.tenant_id,
                queue_id=UUID(work.work_key),
                status="FAILED",
                reason="The page could not be sent for classification. Retry the page.",
                last_error=error,
            )
        elif work.work_type == "CONTROL_EVALUATE" and work.payload.get("unit"):
            mark_unit_controls(
                connection,
                tenant_id=work.tenant_id,
                journey_id=work.journey_id,
                unit=str(work.payload["unit"]),
                status="ERROR_TERMINAL",
                reason="The control could not be evaluated after repeated attempts.",
            )
        elif work.work_type == "SPLIT_BATCH":
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_upload_batches
                    SET batch_status='FAILED', updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                    """
                ),
                {"tenant_id": work.tenant_id, "batch_id": UUID(work.work_key)},
            )
        record_activity(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            event_type="P2_WORK_DEAD_LETTER",
            subject_type="WORK_ITEM",
            subject_id=str(work.work_id),
            details={
                "workType": work.work_type,
                "workKey": work.work_key,
                "attempts": attempts,
                "error": str(exc)[:1800],
            },
            correlation_id=work.correlation_id,
        )


_enqueue = enqueue_work


def _settle_page(
    connection,
    *,
    tenant_id: str,
    queue_id: UUID,
    status: str,
    reason: str | None,
    last_error: str | None = None,
) -> UUID | None:
    batch_id = connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status=:status,
                status_reason=:reason,
                last_error=COALESCE(:last_error, last_error),
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
            RETURNING batch_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "queue_id": queue_id,
            "status": status,
            "reason": reason,
            "last_error": last_error,
        },
    ).scalar_one_or_none()
    if batch_id is not None:
        _refresh_batch_status(connection, tenant_id, UUID(str(batch_id)))
    return UUID(str(batch_id)) if batch_id is not None else None


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
        _owned(connection, work)
        for record in page_records:
            queue_id = uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_document_queue (
                        tenant_id, queue_id, batch_id, journey_id, page_number,
                        page_sha256, page_object_key, client_upload_id,
                        queue_status, correlation_id, unit_kind, page_numbers
                    ) VALUES (
                        :tenant_id, :queue_id, :batch_id, :journey_id, :page_number,
                        :page_sha, :object_key, :client_upload_id,
                        'QUEUED', :correlation_id, 'PAGE', ARRAY[:page_number]
                    )
                    ON CONFLICT (tenant_id, batch_id, page_number) WHERE unit_kind='PAGE'
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
                      AND page_number=:page_number AND unit_kind='PAGE'
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
                    batch_status='PROCESSING',
                    grouping_status=CASE WHEN :page_count > 1 THEN 'PENDING' ELSE 'NOT_NEEDED' END,
                    updated_at_utc=now()
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
        record_activity(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            event_type="UPLOAD_SPLIT_COMPLETE",
            subject_type="UPLOAD_BATCH",
            subject_id=str(batch["batch_id"]),
            details={"pageCount": len(page_records), "sha256": digest},
            correlation_id=work.correlation_id,
        )


def _di_context_and_requirements(engine: Engine, work: WorkItem) -> tuple[str, str, list[str], dict[str, str]]:
    security_client = get_security_oauth_client()
    di_client = get_di_client()
    # Warm the service-token cache before any transaction opens, so the
    # context read below does not wait on Security inside a transaction.
    security_client.get_service_token(audience=_DI_AUDIENCE)
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
        # Candidates: the Journey's own requirement types first (they carry
        # requirement refs), then every template type so mixed PDFs, UPI proofs
        # and optional documents classify instead of landing as UNKNOWN. DI
        # ignores candidates that are not active for the tenant.
        candidates = list(
            dict.fromkeys(_candidate_type_keys(all_requirements) + get_registry().candidate_di_types())
        )
        return (
            context_ref,
            token,
            candidates,
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
                       q.unit_kind, q.page_numbers, q.candidate_override,
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
        if row["queue_status"] in _PAGE_SETTLED_STATES:
            return
        if row["di_document_id"] is not None:
            _enqueue_journey_reconcile(connection, work=work)
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
    # A grouped document was already classified page by page: submit it with
    # exactly that type so DI extracts the whole document as one.
    override = [str(key) for key in (row["candidate_override"] or [])]
    v2_client = get_di_capture_v2_client()
    content_type = "application/pdf" if str(row["page_object_key"]).endswith(".pdf") else str(row["original_content_type"] or "application/octet-stream")
    filename = (
        f"{str(row['original_filename']).rsplit('.', 1)[0]}"
        + (
            f"-pages-{'-'.join(f'{int(n):03d}' for n in row['page_numbers'])}.pdf"
            if row["unit_kind"] == "GROUP"
            else f"-page-{int(row['page_number']):03d}.pdf"
        )
        if content_type == "application/pdf"
        else str(row["original_filename"])
    )

    intent = v2_client.create_upload_intents(
        token=token,
        tenant_id=work.tenant_id,
        external_context_ref=context_ref,
        # DI's capture contract requires a phase; it is only DI's listing
        # partition. P2 never tags a document with a stage: DI's
        # classification picks the document type, the type's template
        # decides the checklist it belongs to, and reconciliation reads
        # both DI phases.
        phase="BOOKING",
        candidate_document_type_keys=override or candidates,
        requirement_refs_by_document_type_key=(
            {k: v for k, v in requirement_refs.items() if k in override} if override else requirement_refs
        ),
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
                    di_state='STORED',
                    di_submitted_at_utc=COALESCE(di_submitted_at_utc, now()),
                    status_reason=NULL,
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
        _enqueue_journey_reconcile(connection, work=work)


def _enqueue_journey_reconcile(connection, *, work: WorkItem, delay_seconds: int = 2) -> None:
    _enqueue(
        connection,
        tenant_id=work.tenant_id,
        journey_id=work.journey_id,
        work_type="JOURNEY_RECONCILE",
        work_key=str(work.journey_id),
        payload={
            "uploadedBy": work.payload.get("uploadedBy"),
            "uploadedByRole": work.payload.get("uploadedByRole"),
        },
        correlation_id=work.correlation_id,
        delay_seconds=delay_seconds,
    )


@dataclass(frozen=True)
class PageOutcome:
    status: str
    reason: str | None


def classify_page_outcome(
    *,
    di_item: dict[str, Any] | None,
    extracted_count: int,
    submitted_at: datetime | None,
    processed_seen_at: datetime | None,
    now: datetime,
) -> PageOutcome:
    """Map DI's durable capture state onto one P2 page state.

    DI capture states: RECEIVING, STORED, CLASSIFYING, CLASSIFIED, UNKNOWN,
    FAILED, DELETED. DI processing (extraction) status: NOT_STARTED,
    PROCESSING, RETRY_PENDING, PROCESSED, FAILED. Facts reach Audit Core via
    the existing document-link sync, so READY means Audit Core has durable
    facts, not merely that DI finished."""
    age = (now - submitted_at).total_seconds() if submitted_at else 0.0
    if extracted_count > 0:
        return PageOutcome("READY", None)
    if di_item is None:
        if age > 300:
            return PageOutcome("FAILED", "The page is no longer known to Document Intelligence. Retry the page.")
        return PageOutcome("CLASSIFYING", None)

    state = str(di_item.get("state") or "")
    processing = str(di_item.get("processingStatus") or "")
    if state == "DELETED":
        return PageOutcome("CANCELLED", "The page was removed from Document Intelligence.")
    if state == "FAILED" or processing == "FAILED":
        return PageOutcome("FAILED", "Document Intelligence could not read this page. Retry or re-upload it.")
    if state == "UNKNOWN":
        # Docket covers, letters, e-mails, printouts: evidence, never a blocker.
        return PageOutcome(
            "SUPPORTING",
            "Kept as supporting evidence. Set the document type if this is a checklist document.",
        )
    if state == "CLASSIFIED" and processing == "PROCESSED":
        waited = (now - processed_seen_at).total_seconds() if processed_seen_at else 0.0
        if waited > _SYNC_GRACE_SECONDS:
            return PageOutcome("NEEDS_REVIEW", "No fields could be extracted from this page.")
        return PageOutcome("SYNCING_TO_AUDIT_CORE", None)
    if age > _PAGE_DEADLINE_SECONDS:
        return PageOutcome("FAILED", "Processing did not finish in time. Retry the page.")
    if state == "CLASSIFIED":
        return PageOutcome("EXTRACTING", None)
    return PageOutcome("CLASSIFYING", None)


def _reconcile_delay(oldest_submitted: datetime | None, now: datetime) -> int:
    age = (now - oldest_submitted).total_seconds() if oldest_submitted else 0.0
    if age < 60:
        return 2
    if age < 300:
        return 5
    return 15


# Pages that have not yet been classified by DI; grouping waits for them.
_PAGE_UNCLASSIFIED_STATES = (
    "QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING",
    "DI_FINALIZING", "CLASSIFYING", "RETRY_WAIT",
)
# Keep re-voiding a merged fragment for a while: a late DI document-link may
# still land after the fragment was retired.
_FRAGMENT_WATCH_MINUTES = 30


def _journey_reconcile(engine: Engine, work: WorkItem) -> None:
    """Reconcile every in-flight unit of one Journey with a single DI listing.

    1. short read: units waiting on DI, merged fragments to retire, batches
       awaiting page grouping
    2. DI listing for both phases + fragment deletes (no transaction open)
    3. short write: apply classification, settle units, void fragment
       evidence, start grouping, request recompute
    """
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        pending = connection.execute(
            text(
                """
                SELECT queue_id, batch_id, di_document_id, queue_status,
                       di_submitted_at_utc, di_processed_seen_at_utc, created_at_utc
                FROM auditcore.p2_document_queue
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND di_document_id IS NOT NULL
                  AND queue_status = ANY(:states)
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "journey_id": work.journey_id,
                "states": list(_PAGE_RECONCILE_STATES),
            },
        ).mappings().all()
        fragments = connection.execute(
            text(
                """
                SELECT queue_id, di_document_id, retired_at_utc
                FROM auditcore.p2_document_queue
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND queue_status='MERGED' AND di_document_id IS NOT NULL
                  AND (retired_at_utc IS NULL
                       OR retired_at_utc > now() - (:watch * interval '1 minute'))
                """
            ),
            {"tenant_id": work.tenant_id, "journey_id": work.journey_id, "watch": _FRAGMENT_WATCH_MINUTES},
        ).mappings().all()
        grouping_due = connection.execute(
            text(
                """
                SELECT b.batch_id
                FROM auditcore.p2_upload_batches b
                WHERE b.tenant_id=:tenant_id AND b.journey_id=:journey_id
                  AND b.grouping_status='PENDING'
                """
            ),
            {"tenant_id": work.tenant_id, "journey_id": work.journey_id},
        ).scalars().all()
    if not pending and not fragments and not grouping_due:
        return

    di_documents: dict[str, dict[str, Any]] = {}
    if pending or fragments:
        context_ref, token, _, _ = _di_context_and_requirements(engine, work)
        v2_client = get_di_capture_v2_client()
        for phase in ("BOOKING", "DELIVERY"):
            listing = v2_client.list_documents(
                token=token,
                tenant_id=work.tenant_id,
                external_context_ref=context_ref,
                phase=phase,
            )
            for item in listing.get("documents") or []:
                di_documents.setdefault(str(item.get("documentId")), item)
        for fragment in fragments:
            if str(fragment["di_document_id"]) in di_documents:
                try:
                    v2_client.delete_document(
                        token=token,
                        tenant_id=work.tenant_id,
                        external_context_ref=context_ref,
                        document_id=str(fragment["di_document_id"]),
                    )
                    di_documents.pop(str(fragment["di_document_id"]), None)
                except Exception:
                    # The original upload is retained and the fragment's
                    # evidence is voided below either way; retry next poll.
                    logger.warning(
                        "p2_fragment_di_delete_failed",
                        tenant_id=work.tenant_id,
                        di_document_id=str(fragment["di_document_id"]),
                        exc_info=True,
                    )

    now = datetime.now(UTC)
    still_waiting = False
    oldest_waiting: datetime | None = None
    facts_changed = False
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        _owned(connection, work)
        if di_documents:
            apply_di_classification(
                connection,
                tenant_id=work.tenant_id,
                journey_id=work.journey_id,
                di_documents=list(di_documents.values()),
                actor_id=str(work.payload.get("uploadedBy") or "SYSTEM"),
                actor_role=str(work.payload.get("uploadedByRole") or "PC"),
                correlation_id=work.correlation_id or "",
            )
        for page in pending:
            outcome = _reconcile_page(
                connection,
                work=work,
                page=dict(page),
                di_item=di_documents.get(str(page["di_document_id"])),
                now=now,
            )
            if outcome.status == "READY":
                facts_changed = True
            if outcome.status not in _PAGE_SETTLED_STATES:
                still_waiting = True
                submitted = page["di_submitted_at_utc"] or page["created_at_utc"]
                if oldest_waiting is None or submitted < oldest_waiting:
                    oldest_waiting = submitted
        if fragments:
            facts_changed = _retire_fragments(connection, work=work, fragments=list(fragments)) or facts_changed
            still_waiting = still_waiting or any(f["retired_at_utc"] is None for f in fragments)
        for batch_id in grouping_due:
            if _start_grouping_when_classified(connection, work=work, batch_id=UUID(str(batch_id))):
                continue
            still_waiting = True
        if facts_changed:
            note_facts_changed(
                connection,
                tenant_id=work.tenant_id,
                journey_id=work.journey_id,
                reason="DOCUMENT_READY",
                correlation_id=work.correlation_id,
            )

    if still_waiting:
        raise RescheduleWork(
            "Waiting for Document Intelligence to finish processing.",
            delay_seconds=_reconcile_delay(oldest_waiting, now),
        )


def _start_grouping_when_classified(connection, *, work: WorkItem, batch_id: UUID) -> bool:
    """Queue BATCH_GROUP once every page of the batch has a classification.
    Returns True when grouping was started."""
    unclassified = connection.execute(
        text(
            """
            SELECT COUNT(*) FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND batch_id=:batch_id AND unit_kind='PAGE'
              AND queue_status = ANY(:states)
            """
        ),
        {"tenant_id": work.tenant_id, "batch_id": batch_id, "states": list(_PAGE_UNCLASSIFIED_STATES)},
    ).scalar_one()
    if unclassified:
        return False
    started = connection.execute(
        text(
            """
            UPDATE auditcore.p2_upload_batches
            SET grouping_status='GROUPING', updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND batch_id=:batch_id AND grouping_status='PENDING'
            RETURNING batch_id
            """
        ),
        {"tenant_id": work.tenant_id, "batch_id": batch_id},
    ).scalar_one_or_none()
    if started is not None:
        _enqueue(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            work_type="BATCH_GROUP",
            work_key=str(batch_id),
            payload=work.payload,
            correlation_id=work.correlation_id,
        )
    return True


def _retire_fragments(connection, *, work: WorkItem, fragments: list[dict[str, Any]]) -> bool:
    """Void evidence of pages merged into a grouped document. Idempotent."""
    stages: set[str] = set()
    for fragment in fragments:
        voided = connection.execute(
            text(
                """
                UPDATE auditcore.evidence
                SET association_status='VOIDED',
                    void_reason='P2_MERGED_INTO_DOCUMENT',
                    voided_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND di_document_id=:document_id AND association_status='ACTIVE'
                RETURNING process_area
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "journey_id": work.journey_id,
                "document_id": fragment["di_document_id"],
            },
        ).scalars().all()
        stages.update(str(stage or "BOOKING").upper() for stage in voided)
        connection.execute(
            text(
                """
                UPDATE auditcore.document_capture_v2_documents
                SET capture_status='SUPERSEDED', updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND di_document_id=:document_id AND capture_status <> 'SUPERSEDED'
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "journey_id": work.journey_id,
                "document_id": fragment["di_document_id"],
            },
        )
        if fragment["retired_at_utc"] is None:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_document_queue SET retired_at_utc=now(), updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                    """
                ),
                {"tenant_id": work.tenant_id, "queue_id": fragment["queue_id"]},
            )
    for stage in sorted(stages):
        _rematerialize_stage(connection, tenant_id=work.tenant_id, journey_id=work.journey_id, stage_code=stage)
    return bool(stages)


def _group_batch(engine: Engine, work: WorkItem) -> None:
    """Combine classified pages of one upload into business documents."""
    batch_id = UUID(work.work_key)
    registry = get_registry()
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        batch = connection.execute(
            text(
                """
                SELECT batch_id, grouping_status, original_filename
                FROM auditcore.p2_upload_batches
                WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                """
            ),
            {"tenant_id": work.tenant_id, "batch_id": batch_id},
        ).mappings().one()
        if batch["grouping_status"] not in {"GROUPING", "PENDING"}:
            return
        pages = connection.execute(
            text(
                """
                SELECT queue_id, page_number, classified_document_type, business_stage,
                       queue_status, page_object_key
                FROM auditcore.p2_document_queue
                WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                  AND unit_kind='PAGE' AND queue_status <> 'MERGED'
                ORDER BY page_number
                """
            ),
            {"tenant_id": work.tenant_id, "batch_id": batch_id},
        ).mappings().all()

    by_number = {int(page["page_number"]): dict(page) for page in pages}
    plan = plan_documents(
        [
            PageFact(
                page_number=int(page["page_number"]),
                di_type=(
                    page["classified_document_type"]
                    if page["queue_status"] not in {"SUPPORTING", "FAILED", "DEAD_LETTER", "CANCELLED"}
                    else None
                ),
                status=str(page["queue_status"]),
                stage=page["business_stage"],
            )
            for page in pages
        ],
        registry,
    )
    grouped = [document for document in plan if document.is_multi_page]

    storage = get_p2_document_storage()
    merged_keys: dict[tuple[int, ...], tuple[str, str]] = {}
    for document in grouped:
        payload = merge_pdf_pages(
            [storage.get_object(str(by_number[n]["page_object_key"])) for n in document.page_numbers]
        )
        digest = hashlib.sha256(payload).hexdigest()
        label = "-".join(f"{n:04d}" for n in document.page_numbers)
        key = (
            f"p2-documents/{work.tenant_id}/{work.journey_id}/{batch_id}/groups/"
            f"{label}-{digest[:12]}.pdf"
        )
        storage.put_object(key, payload, content_type="application/pdf")
        merged_keys[document.page_numbers] = (key, digest)

    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        _owned(connection, work)
        summary = []
        for document in grouped:
            key, digest = merged_keys[document.page_numbers]
            first = by_number[document.page_numbers[0]]
            template = registry.document(document.template_key)
            client_upload_id = f"p2g-{batch_id}-{'-'.join(map(str, document.page_numbers))}-{digest[:12]}"
            group_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_document_queue (
                        tenant_id, batch_id, journey_id, page_number, page_numbers,
                        page_sha256, page_object_key, client_upload_id, queue_status,
                        unit_kind, group_source, candidate_override, classified_document_type,
                        template_key, business_stage, correlation_id
                    ) VALUES (
                        :tenant_id, :batch_id, :journey_id, :first_page, :page_numbers,
                        :digest, :object_key, :client_upload_id, 'QUEUED',
                        'GROUP', 'SYSTEM', CAST(:candidates AS jsonb), :di_type,
                        :template_key, :stage, :correlation_id
                    )
                    ON CONFLICT (tenant_id, batch_id, page_numbers)
                      WHERE unit_kind='GROUP' AND queue_status NOT IN ('CANCELLED','MERGED')
                    DO NOTHING
                    RETURNING queue_id
                    """
                ),
                {
                    "tenant_id": work.tenant_id,
                    "batch_id": batch_id,
                    "journey_id": work.journey_id,
                    "first_page": document.page_numbers[0],
                    "page_numbers": list(document.page_numbers),
                    "digest": digest,
                    "object_key": key,
                    "client_upload_id": client_upload_id,
                    "candidates": json.dumps([document.di_type] if document.di_type else []),
                    "di_type": document.di_type,
                    "template_key": template.key,
                    "stage": first["business_stage"],
                    "correlation_id": work.correlation_id,
                },
            ).scalar_one_or_none()
            if group_id is None:
                group_id = connection.execute(
                    text(
                        """
                        SELECT queue_id FROM auditcore.p2_document_queue
                        WHERE tenant_id=:tenant_id AND batch_id=:batch_id AND unit_kind='GROUP'
                          AND page_numbers=:page_numbers AND queue_status NOT IN ('CANCELLED','MERGED')
                        """
                    ),
                    {"tenant_id": work.tenant_id, "batch_id": batch_id, "page_numbers": list(document.page_numbers)},
                ).scalar_one()
            pages_label = ", ".join(str(n) for n in document.page_numbers)
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_document_queue
                    SET queue_status='MERGED', merged_into_queue_id=:group_id,
                        status_reason=:reason, updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND batch_id=:batch_id AND unit_kind='PAGE'
                      AND page_number = ANY(:page_numbers) AND queue_status <> 'MERGED'
                    """
                ),
                {
                    "tenant_id": work.tenant_id,
                    "batch_id": batch_id,
                    "group_id": group_id,
                    "page_numbers": list(document.page_numbers),
                    "reason": f"Combined into {template.display_name} (pages {pages_label}).",
                },
            )
            _enqueue(
                connection,
                tenant_id=work.tenant_id,
                journey_id=work.journey_id,
                work_type="DOCUMENT_INGEST",
                work_key=str(group_id),
                payload={**work.payload, "queueId": str(group_id), "batchId": str(batch_id)},
                correlation_id=work.correlation_id,
            )
            summary.append({"template": template.key, "pages": list(document.page_numbers)})
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_upload_batches
                SET grouping_status='GROUPED', grouped_at_utc=now(), updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                """
            ),
            {"tenant_id": work.tenant_id, "batch_id": batch_id},
        )
        _refresh_batch_status(connection, work.tenant_id, batch_id)
        record_activity(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            event_type="DOCUMENTS_GROUPED",
            subject_type="UPLOAD_BATCH",
            subject_id=str(batch_id),
            details={
                "pageCount": len(pages),
                "documentCount": len(plan),
                "grouped": summary,
            },
            correlation_id=work.correlation_id,
        )


def _reconcile_page(
    connection,
    *,
    work: WorkItem,
    page: dict[str, Any],
    di_item: dict[str, Any] | None,
    now: datetime,
) -> PageOutcome:
    queue_id = UUID(str(page["queue_id"]))
    di_document_id = UUID(str(page["di_document_id"]))
    extracted_count = int(
        connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
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

    processed_seen_at = page["di_processed_seen_at_utc"]
    if (
        processed_seen_at is None
        and di_item is not None
        and di_item.get("processingStatus") == "PROCESSED"
    ):
        processed_seen_at = now
    outcome = classify_page_outcome(
        di_item=di_item,
        extracted_count=extracted_count,
        submitted_at=page["di_submitted_at_utc"] or page["created_at_utc"],
        processed_seen_at=processed_seen_at,
        now=now,
    )

    if outcome.status == "READY" and not _apply_replacement_lineage(
        connection,
        tenant_id=work.tenant_id,
        journey_id=work.journey_id,
        batch_id=UUID(str(page["batch_id"])),
        new_document_id=di_document_id,
    ):
        # Facts exist but the replacement's evidence row has not been
        # activated yet; keep syncing rather than half-applying lineage.
        outcome = PageOutcome("SYNCING_TO_AUDIT_CORE", None)

    document_type = (
        (local["classified_document_type_key"] if local else None)
        or (di_item or {}).get("classifiedDocumentTypeKey")
    )
    template_key = (
        get_registry().template_for_di_type(
            document_type, stage=(local["stage_code"] if local else None)
        ).key
        if outcome.status not in {"FAILED", "CANCELLED"}
        else None
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status=:status,
                status_reason=:reason,
                classified_document_type=COALESCE(:document_type, classified_document_type),
                template_key=CASE WHEN type_overridden_by_actor_id IS NULL
                                  THEN COALESCE(:template_key, template_key) ELSE template_key END,
                business_stage=COALESCE(:business_stage, business_stage),
                extracted_field_count=:field_count,
                di_state=:di_state,
                di_processing_status=:di_processing_status,
                di_processed_seen_at_utc=:processed_seen_at,
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
              AND (
                queue_status IS DISTINCT FROM :status
                OR status_reason IS DISTINCT FROM :reason
                OR classified_document_type IS DISTINCT FROM COALESCE(:document_type, classified_document_type)
                OR template_key IS DISTINCT FROM COALESCE(:template_key, template_key)
                OR business_stage IS DISTINCT FROM COALESCE(:business_stage, business_stage)
                OR extracted_field_count IS DISTINCT FROM :field_count
                OR di_state IS DISTINCT FROM :di_state
                OR di_processing_status IS DISTINCT FROM :di_processing_status
                OR di_processed_seen_at_utc IS DISTINCT FROM :processed_seen_at
              )
            """
        ),
        {
            "tenant_id": work.tenant_id,
            "queue_id": queue_id,
            "status": outcome.status,
            "reason": outcome.reason,
            "document_type": document_type,
            "template_key": template_key,
            "business_stage": local["stage_code"] if local else None,
            "field_count": extracted_count,
            "di_state": (di_item or {}).get("state"),
            "di_processing_status": (di_item or {}).get("processingStatus"),
            "processed_seen_at": processed_seen_at,
        },
    )

    if outcome.status in _PAGE_SETTLED_STATES and page["queue_status"] not in _PAGE_SETTLED_STATES:
        _refresh_batch_status(connection, work.tenant_id, UUID(str(page["batch_id"])))
        record_activity(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            event_type="DOCUMENT_READY" if outcome.status == "READY" else "DOCUMENT_SETTLED",
            subject_type="DOCUMENT_PAGE",
            subject_id=str(queue_id),
            details={
                "diDocumentId": str(di_document_id),
                "documentType": document_type,
                "fieldCount": extracted_count,
                "status": outcome.status,
                "reason": outcome.reason,
            },
            correlation_id=work.correlation_id,
        )
    return outcome


def _legacy_document_reconcile(engine: Engine, work: WorkItem) -> None:
    """Items queued before 0121 reconcile through the Journey-level path."""
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        _enqueue_journey_reconcile(connection, work=work, delay_seconds=0)


def _apply_replacement_lineage(
    connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    batch_id: UUID,
    new_document_id: UUID,
) -> bool:
    """Supersede the replaced document once the replacement is live.

    Returns False while the replacement has no ACTIVE evidence yet (the
    caller keeps the page syncing); True once lineage is applied or when the
    batch is not a replacement at all."""
    replacement = connection.execute(
        text(
            """
            SELECT replaces_document_id, replaces_evidence_id,
                   replacement_applied_at_utc
            FROM auditcore.p2_upload_batches
            WHERE tenant_id=:tenant_id AND batch_id=:batch_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "batch_id": batch_id},
    ).mappings().one()
    old_document_id = replacement["replaces_document_id"]
    old_evidence_id = replacement["replaces_evidence_id"]
    if old_document_id is None or old_evidence_id is None:
        return True
    if replacement["replacement_applied_at_utc"] is not None:
        return True

    new_evidence = connection.execute(
        text(
            """
            SELECT evidence_id
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND di_document_id=:document_id
              AND association_status='ACTIVE'
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": new_document_id,
        },
    ).scalar_one_or_none()
    if new_evidence is None:
        return False

    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET supersedes_evidence_id=:old_evidence_id
            WHERE tenant_id=:tenant_id AND evidence_id=:new_evidence_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "new_evidence_id": new_evidence,
            "old_evidence_id": old_evidence_id,
        },
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET association_status='SUPERSEDED',
                void_reason='REPLACED_BY_P2_REUPLOAD',
                voided_at_utc=now()
            WHERE tenant_id=:tenant_id
              AND evidence_id=:old_evidence_id
              AND association_status='ACTIVE'
            """
        ),
        {"tenant_id": tenant_id, "old_evidence_id": old_evidence_id},
    )
    old_stage = connection.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_documents
            SET capture_status='SUPERSEDED', updated_at_utc=now()
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND di_document_id=:old_document_id
            RETURNING stage_code
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "old_document_id": old_document_id,
        },
    ).scalar_one_or_none()
    # The superseded document's facts must stop feeding canonical Journey
    # state: re-materialize the stage it belonged to from ACTIVE evidence.
    _rematerialize_stage(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=old_stage)
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_upload_batches
            SET replacement_applied_at_utc=now(), updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND batch_id=:batch_id
            """
        ),
        {"tenant_id": tenant_id, "batch_id": batch_id},
    )
    record_activity(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        event_type="DOCUMENT_REPLACED",
        subject_type="DOCUMENT",
        subject_id=str(new_document_id),
        details={
            "replacesDocumentId": str(old_document_id),
            "newEvidenceId": str(new_evidence),
            "oldEvidenceId": str(old_evidence_id),
        },
    )
    return True


def _rematerialize_stage(connection, *, tenant_id: str, journey_id: UUID, stage_code: str | None) -> None:
    from audit_core.uc03_delivery_post_extraction_materialization import (
        materialize_booking_documents_from_durable_store,
        materialize_delivery_documents_from_durable_store,
    )

    if str(stage_code or "BOOKING").upper() == "DELIVERY":
        materialize_delivery_documents_from_durable_store(
            connection, tenant_id=tenant_id, journey_id=journey_id,
        )
    else:
        materialize_booking_documents_from_durable_store(
            connection, tenant_id=tenant_id, journey_id=journey_id,
        )


def _refresh_batch_status(connection, tenant_id: str, batch_id: UUID) -> None:
    counts = connection.execute(
        text(
            """
            SELECT COUNT(*) FILTER (WHERE queue_status NOT IN ('CANCELLED','MERGED')) AS total,
                   COUNT(*) FILTER (WHERE queue_status IN ('READY','SUPPORTING','NEEDS_REVIEW')) AS usable,
                   COUNT(*) FILTER (WHERE queue_status IN ('FAILED','DEAD_LETTER')) AS failed
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND batch_id=:batch_id
            """
        ),
        {"tenant_id": tenant_id, "batch_id": batch_id},
    ).mappings().one()
    total, usable, failed = int(counts["total"]), int(counts["usable"]), int(counts["failed"])
    if total > 0 and usable == total:
        status = "COMPLETED"
    elif failed > 0 and usable + failed == total:
        status = "PARTIAL_FAILURE" if usable else "FAILED"
    elif total == 0:
        status = "CANCELLED"
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
        _owned(connection, work, lock=False)
        # The engine records STAGE_CHANGED only when the stage actually moves.
        recompute_journey_stage(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
        )
        # Field-review and vehicle-photo work follow the same facts as the gate.
        sync_field_review_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
        sync_vehicle_photo_task(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
        # Gates are fresh: evaluate controls against the same facts.
        request_control_evaluation(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            correlation_id=work.correlation_id,
        )


def _task_verify(engine: Engine, work: WorkItem) -> None:
    """Machine verification of a task the assignee marked as done.

    Never closes a task on the click: the originating condition is
    re-evaluated and the result (in the producer) closes or returns it."""
    task_id = UUID(work.work_key)
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        task = connection.execute(
            text(
                """
                SELECT task_id, source_type, source_code, task_status, completion_protocol
                FROM auditcore.p2_tasks
                WHERE tenant_id=:tenant_id AND task_id=:task_id
                """
            ),
            {"tenant_id": work.tenant_id, "task_id": task_id},
        ).mappings().one()
        if task["task_status"] != "VERIFYING":
            return
        if task["completion_protocol"] != "MACHINE_VERIFIED":
            raise RuntimeError(f"Task {task_id} is VERIFYING but is not MACHINE_VERIFIED")
        source_type = str(task["source_type"] or "")
        if source_type == "DOCUMENT_FIELD":
            sync_field_review_tasks(
                connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                evaluation_started_at=datetime.now(UTC),
            )
            return
        if source_type == "EVIDENCE" and task["source_code"] == "VEHICLE_PHOTOS":
            sync_vehicle_photo_task(
                connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                evaluation_started_at=datetime.now(UTC),
            )
            return
        if source_type != "RULE" or not task["source_code"]:
            raise RuntimeError(
                f"No P2 machine verification adapter for {source_type}:{task['source_code']}"
            )
        if str(task["source_code"]) not in get_registry().controls:
            raise RuntimeError(f"Task {task_id} references unknown control {task['source_code']}")
        # Re-evaluate every unit now (fingerprints are bypassed); the control
        # transition hook closes or returns the task from a fresh result.
        request_control_evaluation(
            connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
            correlation_id=work.correlation_id, delay_seconds=0, force=True,
        )


def _control_evaluate(engine: Engine, work: WorkItem) -> None:
    unit = work.payload.get("unit")
    if unit:
        started_at = datetime.now(UTC)
        transitions = evaluate_unit(
            engine,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            unit=str(unit),
            correlation_id=work.correlation_id,
            force=bool(work.payload.get("force")),
        )
        if transitions:
            on_control_transitions(engine, work=work, transitions=transitions, started_at=started_at)
        return
    # Items queued before the ledger existed name a single control: evaluate
    # every unit instead (units are fingerprint-skipped when nothing changed).
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        request_control_evaluation(
            connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
            correlation_id=work.correlation_id, delay_seconds=0, force=True,
        )


def on_control_transitions(engine: Engine, *, work: WorkItem, transitions: list, started_at: datetime) -> None:
    """Raise, refresh, verify or return machine tasks from fresh control results."""
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        counts = apply_control_transitions(
            connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
            transitions=transitions, evaluation_started_at=started_at,
        )
    if counts:
        logger.info("p2_control_tasks", tenant_id=work.tenant_id, journey_id=str(work.journey_id), **counts)


def process_work(engine: Engine, work: WorkItem) -> None:
    handlers = {
        "SPLIT_BATCH": _split_batch,
        "DOCUMENT_INGEST": _ingest_document,
        "DOCUMENT_RECONCILE": _legacy_document_reconcile,
        "JOURNEY_RECONCILE": _journey_reconcile,
        "BATCH_GROUP": _group_batch,
        "STAGE_RECOMPUTE": _stage_recompute,
        "TASK_VERIFY": _task_verify,
        "CONTROL_EVALUATE": _control_evaluate,
    }
    handler = handlers.get(work.work_type)
    if handler is None:
        raise ValueError(f"Unsupported P2 work type {work.work_type}")
    handler(engine, work)


_SWEEP_CANDIDATES_SQL = text(
    """
    SELECT journey_id FROM auditcore.journey_document_extracted_fields
     WHERE tenant_id=:tenant_id AND updated_at_utc > now() - (:minutes * interval '1 minute')
    UNION
    SELECT journey_id FROM auditcore.payments
     WHERE tenant_id=:tenant_id AND updated_at_utc > now() - (:minutes * interval '1 minute')
    UNION
    SELECT journey_id FROM auditcore.workflow_tasks
     WHERE tenant_id=:tenant_id AND task_type='MANUAL_VERIFICATION_REVIEW'
       AND updated_at_utc > now() - (:minutes * interval '1 minute')
    """
)


def _fact_sweep(engine: Engine, tenant_id: str) -> int:
    """Safety net for fact changes made outside P2 code paths.

    Journeys touched in the recent window are fingerprinted; only a real
    change against the stored fingerprint requests recomputation, so the
    sweep is idempotent and cheap. The window is deliberately wider than the
    sweep interval so a long-running writer transaction (whose updated_at is
    its start time) is still observed after it commits."""
    changed = 0
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys = connection.execute(
            _SWEEP_CANDIDATES_SQL,
            {"tenant_id": tenant_id, "minutes": _FACT_SWEEP_WINDOW_MINUTES},
        ).scalars().all()
        for journey_id in journeys:
            current = fact_fingerprint(connection, tenant_id=tenant_id, journey_id=journey_id)
            stored = connection.execute(
                text(
                    """
                    SELECT fact_fingerprint FROM auditcore.p2_journey_runtime
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).scalar_one_or_none()
            if stored == current:
                continue
            note_facts_changed(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                reason="FACT_SWEEP",
            )
            changed += 1
    return changed


_last_sweep_at: dict[str, float] = {}


def _maybe_sweep(engine: Engine, tenant_id: str) -> None:
    now = time.monotonic()
    if now - _last_sweep_at.get(tenant_id, 0.0) < _FACT_SWEEP_SECONDS:
        return
    _last_sweep_at[tenant_id] = now
    try:
        changed = _fact_sweep(engine, tenant_id)
        if changed:
            logger.info("p2_fact_sweep_changes", tenant_id=tenant_id, journeys=changed)
    except Exception:
        logger.warning("p2_fact_sweep_failed", tenant_id=tenant_id, exc_info=True)


def _settle(engine: Engine, work: WorkItem, action, *args) -> None:
    try:
        action(engine, work, *args)
    except LeaseLost:
        logger.warning(
            "p2_work_lease_lost",
            tenant_id=work.tenant_id,
            work_type=work.work_type,
            work_key=work.work_key,
        )


def run_once(engine: Engine | None = None) -> int:
    engine = engine or get_engine()
    claimed: list[WorkItem] = []
    for tenant_id in _active_tenants(engine):
        _maybe_sweep(engine, tenant_id)
        remaining = _WORKER_CONCURRENCY - len(claimed)
        if remaining > 0:
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
                _settle(engine, work, _reschedule, exc)
            except LeaseLost:
                logger.warning(
                    "p2_work_lease_lost",
                    tenant_id=work.tenant_id,
                    work_type=work.work_type,
                    work_key=work.work_key,
                )
            except Exception as exc:
                logger.warning(
                    "p2_work_failed",
                    tenant_id=work.tenant_id,
                    journey_id=str(work.journey_id),
                    work_type=work.work_type,
                    work_key=work.work_key,
                    attempt=work.attempt_count + 1,
                    exc_info=True,
                )
                _settle(engine, work, _fail, exc)
            else:
                _settle(engine, work, _complete)
    return len(claimed)


def main() -> None:
    engine = get_engine()
    logger.info(
        "p2_worker_started",
        worker_id=_WORKER_ID,
        concurrency=_WORKER_CONCURRENCY,
        per_journey_concurrency=_PER_JOURNEY_CONCURRENCY,
    )
    while True:
        try:
            processed = run_once(engine)
        except Exception:
            # A database blip must not kill the worker process.
            logger.warning("p2_worker_cycle_failed", exc_info=True)
            processed = 0
        if processed == 0:
            time.sleep(_POLL_SECONDS)


if __name__ == "__main__":
    main()
