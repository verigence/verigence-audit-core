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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import structlog
from httpx import HTTPStatusError, TransportError
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from sqlalchemy import Engine, text

from audit_core.config import SettingsError, load_settings
from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import get_engine
from audit_core.di_capture_v2_client import DiCaptureV2Error
from audit_core.logging_config import configure_logging, exception_summary, redact_text
from audit_core.otel import install_correlation_propagation
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
from audit_core.uc03_p2_customer import sync_customer_name
from audit_core.uc03_p2_grouping import (
    PageFact,
    merge_pdf_pages,
    needs_document_upload,
    plan_documents,
    read_once_di_types,
)
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_runtime import (
    enqueue_work,
    fact_fingerprint,
    note_facts_changed,
    record_activity,
    request_page_recovery,
    requeue_page_for_ingest,
)
from audit_core.uc03_p2_stage import recompute_journey_stage
from audit_core.uc03_p2_storage import get_p2_document_storage
from audit_core.uc03_p2_task_producer import (
    apply_control_transitions,
    sync_booking_date_task,
    sync_delivery_review_task,
    sync_document_missing_tasks,
    sync_field_review_tasks,
    sync_name_consistency_tasks,
    sync_processing_failure_tasks,
    sync_unclassified_page_tasks,
    sync_vehicle_photo_task,
)
from audit_core.uc03_p2_workflow import mark_delivery_reviewed
from audit_core.uc03_unified_document_capture import (
    _merged_candidate_requirements,
    _receipt_defaults_to_delivery,
    _requirements_owned_by_stage,
    apply_di_classification,
)

logger = structlog.get_logger(__name__)

# Retries for a step that hit a transient fault (the document service or
# storage down, rate limited): two quick ones for a blip, two spaced ones
# for an outage, then the PC is told. A fault that cannot recover (the
# document service refused the request, an unreadable file) is told at once.
_RETRY_DELAYS_SECONDS = (60, 240, 1800, 3600)
_MAX_ATTEMPTS = int(os.environ.get("P2_WORKER_MAX_ATTEMPTS", str(len(_RETRY_DELAYS_SECONDS) + 1)))
_POLL_SECONDS = float(os.environ.get("P2_WORKER_POLL_SECONDS", "1.0"))
_MAX_PDF_PAGES = int(os.environ.get("P2_MAX_PDF_PAGES", "100"))
_WORKER_CONCURRENCY = max(1, int(os.environ.get("P2_WORKER_CONCURRENCY", "6")))
_PER_JOURNEY_CONCURRENCY = max(1, int(os.environ.get("P2_PER_JOURNEY_CONCURRENCY", "3")))
_LEASE_SECONDS = int(os.environ.get("P2_WORKER_LEASE_SECONDS", "600"))
_LEASE_RENEW_SECONDS = max(5, _LEASE_SECONDS // 3)
# A page is never failed for taking long (decision 2026-09-30: load is
# never a failure). While Document Intelligence still has it in hand it is
# polled, ever more slowly (_reconcile_delay); the file's status task tells
# the PC after an hour, and the nightly sweep looks again after a day. Only
# DI's own verdict, or DI no longer knowing the page, fails it.
# A page the PC or the nightly sweep asked to recover from FAILED is looked
# up at DI first (_recover_page); a technical extraction failure is left to
# DI's own nightly reprocessing for this long before the page is uploaded
# once more.
_RECOVERY_REUPLOAD_AFTER_SECONDS = 4 * 24 * 3600
# DI reports PROCESSED before the document-link sync has copied facts into
# Audit Core. Allow the sync this long before treating "no facts" as a result.
_FACT_SWEEP_SECONDS = float(os.environ.get("P2_FACT_SWEEP_SECONDS", "30"))
_FACT_SWEEP_WINDOW_MINUTES = int(os.environ.get("P2_FACT_SWEEP_WINDOW_MINUTES", "10"))
# When (UTC, HH:MM) the nightly review of deliveries in progress is queued.
_NIGHTLY_REVIEW_UTC = os.environ.get("P2_NIGHTLY_REVIEW_UTC", "20:30")
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# Page states that still need DI/Audit Core progress.
_PAGE_ACTIVE_STATES = (
    "QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING",
    "DI_FINALIZING", "CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE",
    "RETRY_WAIT",
)
# A page in RETRY_WAIT is waiting for its ingest step to run again; the
# reconcile sweep leaves it alone until then.
# NEEDS_REVIEW is settled (no waiting loop), but it is re-read on every
# reconcile: a page marked "no fields" because the fact sync ran late turns
# READY as soon as its facts are in, instead of staying wrong for good.
_PAGE_RECONCILE_STATES = ("CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE", "NEEDS_REVIEW")
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
            item = _work_item(row, attempts=attempts, token=token)
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
                logger.error(
                    "p2_work_dead_lettered",
                    reason_code="LEASE_EXPIRED_REPEATEDLY",
                    error_category="TECHNICAL",
                    **_work_fields(item),
                )
                _mark_dead(
                    connection, item, attempts=attempts, retryable=True,
                    cause="Processing of this item was interrupted repeatedly.",
                    error="LeaseExpired: Worker lease expired repeatedly while processing.",
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
            claimed.append(item)
    return claimed


def _work_item(row: Any, *, attempts: int, token: UUID) -> WorkItem:
    return WorkItem(
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


def _work_correlation_id(work: WorkItem) -> str:
    # System-queued work (sweeps, recomputes) has no originating request; give it a stable id.
    return work.correlation_id or f"p2w-{work.work_id}"


def _work_fields(work: WorkItem) -> dict[str, Any]:
    """The identifiers every work log line carries."""
    return {
        "correlation_id": _work_correlation_id(work),
        "tenant_id": work.tenant_id,
        "journey_id": str(work.journey_id),
        "work_id": str(work.work_id),
        "work_type": work.work_type,
        "work_key": work.work_key,
        "attempt": work.attempt_count + 1,  # the attempt being made (1-based)
    }


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


def _di_error_body(exc: DiCaptureV2Error) -> dict[str, Any]:
    try:
        body = json.loads(exc.detail)
    except (TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _is_retryable(exc: Exception) -> bool:
    """Whether trying again can help: the document service says so when it
    answers, else its status code does; an unreadable file never recovers."""
    if isinstance(exc, DiCaptureV2Error):
        flag = _di_error_body(exc).get("retryable")
        if isinstance(flag, bool):
            return flag
        return exc.status_code in (408, 425, 429) or exc.status_code >= 500
    # A programming error fails the same way every time; retrying for an hour and a half only
    # delays the PC's task. It is logged as TECHNICAL with its location.
    return not isinstance(exc, (ValueError, *_PROGRAMMING_ERRORS))


_PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError, KeyError, IndexError, AssertionError, NotImplementedError)


def _plain_cause(exc: Exception) -> str:
    """Why the step failed, in words a PC can act on."""
    if isinstance(exc, DiCaptureV2Error):
        body = _di_error_body(exc)
        what = body.get("detail") or body.get("title")
        if isinstance(what, str) and what.strip():
            return f"The document service could not accept this page: {redact_text(what, 200)}"
        return "The document service could not accept this page."
    if isinstance(exc, ValueError):
        # Our own ValueErrors describe the file ("Encrypted PDF is not supported", ...).
        return redact_text(str(exc)) or "The file could not be read."
    return "The page could not be processed because of a system problem."


def _delay_text(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600} hour" + ("" if seconds < 7200 else "s")
    return f"{max(1, seconds // 60)} minute" + ("" if seconds < 120 else "s")


def _fail(engine: Engine, work: WorkItem, exc: Exception) -> None:
    attempts = work.attempt_count + 1
    retryable = _is_retryable(exc)
    terminal = (not retryable) or attempts >= _MAX_ATTEMPTS
    delay = _RETRY_DELAYS_SECONDS[min(attempts, len(_RETRY_DELAYS_SECONDS)) - 1]
    cause = _plain_cause(exc)
    summary = exception_summary(exc)
    # Stored and shown in the batch view: class + redacted message + where it failed.
    error = summary["exc_type"] + (f": {summary['exc_message']}" if "exc_message" in summary else "")
    error = error[:900]
    if summary["exc_stack"]:
        error += f" (at {summary['exc_stack'][-1]})"
    fields = {**_work_fields(work), "retryable": retryable, **summary}
    if terminal:
        logger.error("p2_work_dead_lettered", error_category=_failure_category(exc), **fields)
    else:
        logger.warning("p2_work_retry_scheduled", error_category=_failure_category(exc),
                       retry_in_seconds=delay, **fields)
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
            if work.work_type == "DOCUMENT_INGEST":
                _settle_page(
                    connection,
                    tenant_id=work.tenant_id,
                    queue_id=UUID(work.work_key),
                    status="RETRY_WAIT",
                    reason=(f"{cause} Retrying automatically in {_delay_text(delay)} (attempt {attempts} of "
                            f"{_MAX_ATTEMPTS}). You can leave this page and check back later."),
                    last_error=error,
                )
                # The PC is told at once: check back in an hour; the same
                # task turns into "upload this page on its own" if the
                # retries run out, and closes itself if one succeeds.
                sync_processing_failure_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
            return
        _mark_dead(connection, work, attempts=attempts, retryable=retryable, cause=cause, error=error)


def _failure_category(exc: Exception) -> str:
    """BUSINESS: the input itself can't be processed (bad/encrypted file, DI rejected it).
    DEPENDENCY: DI or storage unavailable. TECHNICAL: our own failure."""
    if isinstance(exc, DiCaptureV2Error):
        return "DEPENDENCY" if _is_retryable(exc) else "BUSINESS"
    if isinstance(exc, ValueError):
        return "BUSINESS"
    if isinstance(exc, (TransportError, HTTPStatusError)):
        return "DEPENDENCY"
    return "TECHNICAL"


def _mark_dead(connection, work: WorkItem, *, attempts: int, retryable: bool, cause: str, error: str) -> None:
    """Terminal outcome for a work item: settle what it was working on, raise the PC's
    processing-failure task and record the activity."""
    if work.work_type == "DOCUMENT_INGEST":
        _settle_page(
            connection,
            tenant_id=work.tenant_id,
            queue_id=UUID(work.work_key),
            status="FAILED",
            reason=(f"{cause} " + (f"Not processed after {attempts} attempts. " if retryable else "")
                    + "Retry the page, remove the upload, or delete this booking."),
            last_error=error,
        )
        sync_processing_failure_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
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
        sync_processing_failure_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
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
            "error": error,
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
              AND queue_status <> 'MERGED'
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


def _page_content_sha(payload: bytes, content_type: str) -> str:
    """A page's identity for duplicate detection: the scan image inside a
    PDF page when it holds exactly one, else the bytes as uploaded. The same
    photo combined into two different PDFs then hashes the same."""
    if content_type == "application/pdf":
        try:
            page = PdfReader(io.BytesIO(payload)).pages[0]
            resources = page.get("/Resources")
            resources = resources.get_object() if hasattr(resources, "get_object") else resources
            xobjects = (resources or {}).get("/XObject")
            xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
            images = [x.get_object() for x in (xobjects or {}).values()]
            images = [x for x in images if x.get("/Subtype") == "/Image"]
            if len(images) == 1:
                return hashlib.sha256(images[0].get_data()).hexdigest()
        except Exception:  # noqa: BLE001, S110 - an odd PDF page is hashed as uploaded
            pass
    return hashlib.sha256(payload).hexdigest()


def _single_page_pdf(page) -> bytes:
    writer = PdfWriter()
    writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _refuse_duplicate_upload(engine: Engine, work: WorkItem, *, batch_id: UUID, digest: str) -> bool:
    """The same file uploaded again (byte for byte) is refused: the batch is
    cancelled before any page is queued, and the Journey's history says
    which earlier upload it repeats. True when refused."""
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        earlier = connection.execute(
            text(
                """
                SELECT batch_id, original_filename, created_at_utc
                FROM auditcore.p2_upload_batches
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND sha256=:sha256
                  AND batch_id<>:batch_id AND batch_status NOT IN ('FAILED','CANCELLED')
                ORDER BY created_at_utc ASC LIMIT 1
                """
            ),
            {"tenant_id": work.tenant_id, "journey_id": work.journey_id, "sha256": digest, "batch_id": batch_id},
        ).mappings().first()
        if earlier is None:
            return False
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_upload_batches
                SET sha256=:sha256, batch_status='CANCELLED', updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND batch_id=:batch_id
                """
            ),
            {"tenant_id": work.tenant_id, "batch_id": batch_id, "sha256": digest},
        )
        record_activity(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            event_type="UPLOAD_DUPLICATE",
            subject_type="UPLOAD_BATCH",
            subject_id=str(batch_id),
            details={"duplicateOf": str(earlier["batch_id"]), "filename": earlier["original_filename"],
                     "uploadedAtUtc": earlier["created_at_utc"].isoformat(), "sha256": digest},
            correlation_id=work.correlation_id,
        )
        logger.info("p2_upload_duplicate_refused", tenant_id=work.tenant_id, journey_id=str(work.journey_id),
                    batch_id=str(batch_id), duplicate_of=str(earlier["batch_id"]))
        return True


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
    if _refuse_duplicate_upload(engine, work, batch_id=UUID(str(batch["batch_id"])), digest=digest):
        return
    content_type = str(batch["content_type"] or "")
    filename = str(batch["original_filename"])
    is_pdf = content_type.lower() == "application/pdf" or filename.lower().endswith(".pdf")

    pages: list[tuple[bytes, str]] = []
    if is_pdf:
        try:
            reader = PdfReader(io.BytesIO(payload))
        except PdfReadError as exc:
            raise ValueError("The PDF could not be read; it may be damaged. Upload it again.") from exc
        if reader.is_encrypted:
            # An empty user password opens "protected but not locked" PDFs; anything else is the
            # file, not the platform. (A missing crypto library would raise DependencyError and
            # stay a technical failure.)
            try:
                opened = reader.decrypt("")
            except (PdfReadError, NotImplementedError) as exc:
                raise ValueError("Encrypted PDF is not supported") from exc
            if not opened:
                raise ValueError("Encrypted PDF is not supported")
        if not 1 <= len(reader.pages) <= _MAX_PDF_PAGES:
            raise ValueError(f"PDF page count {len(reader.pages)} is outside allowed range")
        pages = [(_single_page_pdf(page), "application/pdf") for page in reader.pages]
    else:
        pages = [(payload, content_type)]

    page_records: list[dict[str, Any]] = []
    for page_number, (page_payload, page_content_type) in enumerate(pages, start=1):
        page_sha = _page_content_sha(page_payload, page_content_type)
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
        # The same scan already on this Journey (an earlier upload, or an
        # earlier page of this file) is recorded, shown, and not sent again.
        already = {
            str(r["page_sha256"]): (int(r["page_number"]), str(r["original_filename"]))
            for r in connection.execute(
                text(
                    """
                    SELECT DISTINCT ON (q.page_sha256) q.page_sha256, q.page_number, b.original_filename
                    FROM auditcore.p2_document_queue q
                    JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
                    WHERE q.tenant_id=:tenant_id AND q.journey_id=:journey_id AND q.batch_id<>:batch_id
                      AND q.unit_kind='PAGE' AND q.page_sha256 = ANY(:shas)
                      AND q.queue_status NOT IN ('FAILED','DEAD_LETTER','CANCELLED')
                    ORDER BY q.page_sha256, q.created_at_utc
                    """
                ),
                {"tenant_id": work.tenant_id, "journey_id": work.journey_id, "batch_id": batch["batch_id"],
                 "shas": [r["pageSha"] for r in page_records]},
            ).mappings().all()
        }
        # A failed page uploaded again (decision 2026-09-30): the fresh copy
        # is the one that counts. The old FAILED page is cancelled as
        # superseded, so its file's task closes on its own, and the new page
        # is processed.
        superseded_batches: set[str] = set()
        for record in page_records:
            if record["pageSha"] in already:
                continue
            for row in connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_document_queue
                    SET queue_status='CANCELLED', last_error='SUPERSEDED_PAGE', status_reason=:reason,
                        updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND batch_id<>:batch_id
                      AND unit_kind='PAGE' AND page_sha256=:sha AND queue_status IN ('FAILED','DEAD_LETTER')
                    RETURNING batch_id
                    """
                ),
                {"tenant_id": work.tenant_id, "journey_id": work.journey_id, "batch_id": batch["batch_id"],
                 "sha": record["pageSha"],
                 "reason": (f"Uploaded again as page {record['pageNumber']} of {batch['original_filename']}; "
                            "this copy is replaced.")},
            ).mappings().all():
                superseded_batches.add(str(row["batch_id"]))
        for superseded_batch in superseded_batches:
            _refresh_batch_status(connection, work.tenant_id, UUID(superseded_batch))
        if superseded_batches:
            sync_processing_failure_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
        queued = 0
        for record in page_records:
            queue_id = uuid4()
            duplicate_of = already.get(record["pageSha"])
            if duplicate_of is None:
                already[record["pageSha"]] = (record["pageNumber"], str(batch["original_filename"]))
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_document_queue (
                        tenant_id, queue_id, batch_id, journey_id, page_number,
                        page_sha256, page_object_key, client_upload_id,
                        queue_status, status_reason, last_error, correlation_id, unit_kind, page_numbers
                    ) VALUES (
                        :tenant_id, :queue_id, :batch_id, :journey_id, :page_number,
                        :page_sha, :object_key, :client_upload_id,
                        :queue_status, :status_reason, :last_error, :correlation_id, 'PAGE', ARRAY[:page_number]
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
                    "queue_status": "CANCELLED" if duplicate_of else "QUEUED",
                    "status_reason": (
                        f"Same as page {duplicate_of[0]} of {duplicate_of[1]}; already uploaded, not sent again."
                        if duplicate_of else None
                    ),
                    "last_error": "DUPLICATE_PAGE" if duplicate_of else None,
                    "correlation_id": work.correlation_id,
                },
            )
            if duplicate_of:
                continue
            queued += 1
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
        if queued == 0:
            # Every page was already on the Journey: nothing to process.
            _refresh_batch_status(connection, work.tenant_id, UUID(str(batch["batch_id"])))
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


# Conditional documents Phase 2 makes mandatory from the deal's evidence that
# the Phase 1 requirement catalog never had. DI reads a document, and links
# its facts back, only against a requirement row -- so each gets an OPTIONAL
# Delivery row (idempotent, like seed_delivery_document_requirements' own
# fixed rows). Whether it is required is decided by the P2 templates.
_P2_ONLY_REQUIREMENTS = (
    ("p2_bank_approval_letter", "bank_approval_letter"),
    ("p2_purchase_order", "purchase_order"),
    ("p2_debit_note", "debit_note"),
    ("p2_valuation_report", "valuation_report"),
)


def _ensure_p2_requirement_rows(connection, *, tenant_id: str, journey_id: UUID) -> None:
    for requirement_key, document_type_key in _P2_ONLY_REQUIREMENTS:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, document_requirement_item_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, requirement_status, condition_snapshot
                )
                SELECT CAST(:t AS varchar), CAST(:j AS uuid), NULL, CAST(:rk AS varchar),
                       CAST(:dt AS varchar), 'DELIVERY', 'OPTIONAL', 'PENDING', '{}'::jsonb
                WHERE NOT EXISTS (
                    SELECT 1 FROM auditcore.journey_document_requirements
                    WHERE tenant_id=CAST(:t AS varchar) AND journey_id=CAST(:j AS uuid)
                      AND document_type_key=CAST(:dt AS varchar)
                )
                ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
                """
            ),
            {"t": tenant_id, "j": journey_id, "rk": requirement_key, "dt": document_type_key},
        )


def _di_context_and_requirements(engine: Engine, work: WorkItem) -> tuple[str, str, list[str], dict[str, str]]:
    security_client = get_security_oauth_client()
    di_client = get_di_client()
    # Warm the service-token cache before any transaction opens, so the
    # context read below does not wait on Security inside a transaction.
    security_client.get_service_token(audience=_DI_AUDIENCE)
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        _ensure_p2_requirement_rows(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
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
                       q.di_submitted_at_utc,
                       q.unit_kind, q.page_numbers, q.candidate_override,
                       b.original_filename, b.content_type AS original_content_type,
                       b.uploaded_by_actor_id, b.page_count, b.grouping_status
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
        if work.payload.get("recover") and row["di_document_id"] is not None:
            recover = dict(row)
        elif row["di_document_id"] is not None:
            _enqueue_journey_reconcile(connection, work=work)
            return
        else:
            recover = None
    if recover is not None:
        _recover_page(engine, work, recover)
        return
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
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
    # exactly that type so DI extracts the whole document as one -- and, the
    # type being known, without paying for a second classification.
    override = [str(key) for key in (row["candidate_override"] or [])]
    classification_mode = "TRUST_SINGLE_CANDIDATE" if len(override) == 1 else None
    if (
        not override
        and row["unit_kind"] == "PAGE"
        and int(row["page_count"] or 1) > 1
        and row["grouping_status"] in ("PENDING", "GROUPING")
    ):
        # A page of a multi-page upload whose type is always merged is only
        # classified here (no requirement ref: DI does not extract it); the
        # merged document is extracted once (_group_batch). A page retried
        # after grouping stands alone and is read in full.
        read_once = read_once_di_types(get_registry())
        requirement_refs = {k: v for k, v in requirement_refs.items() if k not in read_once}
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
        classification_mode=classification_mode,
        # DI's capture contract takes the file's identity only; it measures
        # the bytes itself on upload and rejects any other field.
        files=[{
            "clientUploadId": str(row["client_upload_id"]),
            "filename": filename,
            "contentType": content_type,
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
                    di_submitted_at_utc=now(),
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


_QUALITY_FAILURE_PREFIX = "DI_QUALITY_"
_UNREADABLE_FAILURE_CODES = frozenset({"FILE_EMPTY", "INVALID_FILE_CONTENT", "CORRUPT", "UPLOAD_FAILED"})


def recovery_decision(
    *,
    di_item: dict[str, Any] | None,
    marker: str,
    submitted_at: datetime | None,
    now: datetime,
) -> str:
    """What to do with a page the PC (marker "r") or the nightly sweep
    (marker "n") asked to recover, given what Document Intelligence holds
    for it: a page state to poll from ("SYNCING_TO_AUDIT_CORE" when DI has
    read it, so the worker copies; "EXTRACTING" or "CLASSIFYING" while DI
    still has it in hand), "REUPLOAD" when DI has given it up or no longer
    knows it, or "LEAVE" for the nightly sweep meeting a technical
    extraction failure DI's own nightly reprocessing is still going to
    retry (for _RECOVERY_REUPLOAD_AFTER_SECONDS after submission)."""
    if di_item is None:
        return "REUPLOAD"
    state = str(di_item.get("state") or "")
    processing = str(di_item.get("processingStatus") or "")
    if state in ("FAILED", "DELETED"):
        return "REUPLOAD"
    if state == "CLASSIFIED":
        if processing == "PROCESSED":
            return "SYNCING_TO_AUDIT_CORE"
        if processing == "FAILED":
            code = str(di_item.get("failureCode") or "").upper()
            final = code.startswith(_QUALITY_FAILURE_PREFIX) or code in _UNREADABLE_FAILURE_CODES
            if final or marker != "n":
                return "REUPLOAD"
            age = (now - submitted_at).total_seconds() if submitted_at else float("inf")
            return "REUPLOAD" if age > _RECOVERY_REUPLOAD_AFTER_SECONDS else "LEAVE"
        return "EXTRACTING"
    return "CLASSIFYING"


def _recover_page(engine: Engine, work: WorkItem, row: dict[str, Any]) -> None:
    """A FAILED page the PC or the nightly sweep asked to recover: look at
    what Document Intelligence already holds for it before ever uploading
    it again (decision 2026-09-30: never re-read what DI already read)."""
    marker = str(work.payload.get("recover"))
    queue_id = UUID(work.work_key)
    context_ref, token, _, _ = _di_context_and_requirements(engine, work)
    v2_client = get_di_capture_v2_client()
    di_item: dict[str, Any] | None = None
    for phase in ("BOOKING", "DELIVERY"):
        listing = v2_client.list_documents(
            token=token, tenant_id=work.tenant_id, external_context_ref=context_ref, phase=phase,
        )
        for item in listing.get("documents") or []:
            if str(item.get("documentId")) == str(row["di_document_id"]):
                di_item = item
    decision = recovery_decision(
        di_item=di_item, marker=marker, submitted_at=row.get("di_submitted_at_utc"), now=datetime.now(UTC),
    )
    logger.info(
        "p2_page_recovery",
        tenant_id=work.tenant_id,
        journey_id=str(work.journey_id),
        queue_id=str(queue_id),
        di_document_id=str(row["di_document_id"]),
        marker=marker,
        di_state=(di_item or {}).get("state"),
        di_processing_status=(di_item or {}).get("processingStatus"),
        decision=decision,
    )
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        if decision == "REUPLOAD":
            requeue_page_for_ingest(
                connection, tenant_id=work.tenant_id, journey_id=work.journey_id, queue_id=queue_id,
                client_upload_id=str(row["client_upload_id"]), marker=marker,
                requested_by=str(work.payload.get("uploadedBy") or "SYSTEM"), correlation_id=work.correlation_id,
            )
        elif decision == "LEAVE":
            _settle_page(
                connection, tenant_id=work.tenant_id, queue_id=queue_id, status="FAILED",
                reason=_di_failure_reason(di_item),
            )
        else:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_document_queue
                    SET queue_status=:status, status_reason=NULL, updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                    """
                ),
                {"tenant_id": work.tenant_id, "queue_id": queue_id, "status": decision},
            )
            _enqueue_journey_reconcile(connection, work=work)


def _di_failure_reason(di_item: dict[str, Any] | None) -> str:
    """Why the document service failed a page, as the PC should read it: a
    scan-quality rejection is final and needs a re-scan, not a retry."""
    code = str((di_item or {}).get("failureCode") or "").upper()
    detail = str((di_item or {}).get("failureDetail") or "").strip().rstrip(".")
    if code.startswith(_QUALITY_FAILURE_PREFIX):
        return (f"Page rejected: {detail or 'it did not pass the scan quality check'}. "
                "Re-scan this page and upload it again; retrying will not help.")
    if code in _UNREADABLE_FAILURE_CODES:
        return f"The file could not be read{': ' + detail if detail else ''}. Upload it again."
    return "Document Intelligence could not read this page. Retry or re-upload it." + (f" ({detail})" if detail else "")


# Set on a page once the worker itself copied DI's result and there was
# nothing in it. Distinct from the wording the old timer-based path used, so
# the nightly sweep can tell a page that was genuinely read empty from one
# that was merely never copied.
_NOTHING_READ_REASON = "Document Intelligence read this page but found no values on it."
_EXTRA_COPY_REASON = "Read, but this booking already has this document; kept as an extra copy."


def classify_page_outcome(
    *,
    di_item: dict[str, Any] | None,
    extracted_count: int,
    submitted_at: datetime | None,
    facts_copy: str | None,
    now: datetime,
) -> PageOutcome:
    """Map DI's durable capture state onto one P2 page state.

    DI capture states: RECEIVING, STORED, CLASSIFYING, CLASSIFIED, UNKNOWN,
    FAILED, DELETED. DI processing (extraction) status: NOT_STARTED,
    PROCESSING, RETRY_PENDING, PROCESSED, FAILED. READY means Audit Core
    holds the page's values. ``facts_copy`` is what this reconcile's own
    copy of DI's result did (_copy_page_facts): "COPIED" (the values, or
    the absence of any, are durable now), "UNLINKED" (nothing to hold them
    against: the booking already has this document) or None (the copy did
    not run this round, look again shortly)."""
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
        return PageOutcome("FAILED", _di_failure_reason(di_item))
    if state == "UNKNOWN":
        # Docket covers, letters, e-mails, printouts: evidence, never a blocker.
        return PageOutcome(
            "SUPPORTING",
            "Kept as supporting evidence. Set the document type if this is a checklist document.",
        )
    if state == "CLASSIFIED" and processing == "PROCESSED":
        if facts_copy == "COPIED":
            return PageOutcome("NEEDS_REVIEW", _NOTHING_READ_REASON)
        if facts_copy == "UNLINKED":
            return PageOutcome("SUPPORTING", _EXTRA_COPY_REASON)
        return PageOutcome("SYNCING_TO_AUDIT_CORE", None)
    if state == "CLASSIFIED":
        return PageOutcome("EXTRACTING", None)
    return PageOutcome("CLASSIFYING", None)


def _copy_page_facts(
    engine: Engine,
    *,
    work: WorkItem,
    page: dict[str, Any],
    di_item: dict[str, Any],
    requirement_refs: dict[str, str],
) -> str | None:
    """Copy a processed page's values from Document Intelligence into Audit
    Core, right here in the reconcile, instead of waiting for DI's one-shot
    link callback and the in-memory background task it used to trigger
    (lost on a redeploy, dropped after lock contention, never retried on a
    non-retryable reply). A plain read of a result DI already holds: no
    model call, ever. Returns "COPIED" once the values (or the absence of
    any) are durable, "UNLINKED" when the booking has no slot left for this
    document (an extra copy), None when the copy could not run this round
    (per-journey lock busy, DI or Security unreachable) and should be tried
    again on the next poll."""
    from audit_core.uc03_confidence_review_policy import (
        DocumentSyncLockBusyError,
        _prefetch_document_for_sync,
        _sync_booking_document_once,
    )
    from audit_core.uc03_document_capture_v2 import _ensure_evidence_link_for_resync

    document_id = UUID(str(page["di_document_id"]))
    document_type = str(di_item.get("classifiedDocumentTypeKey") or "")
    requirement_ref = requirement_refs.get(document_type)
    try:
        with engine.begin() as connection:
            set_tenant_context(connection, work.tenant_id)
            held = connection.execute(
                text(
                    """
                    SELECT COUNT(*) FROM auditcore.journey_document_extracted_fields
                    WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d
                    """
                ),
                {"t": work.tenant_id, "j": work.journey_id, "d": document_id},
            ).scalar_one()
            if int(held or 0) > 0:
                return "COPIED"
            if requirement_ref is not None:
                linked = _ensure_evidence_link_for_resync(
                    connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                    requirement_ref=requirement_ref, document_id=document_id, service_id="p2-worker",
                )
            else:
                linked = bool(connection.execute(
                    text(
                        """
                        SELECT 1 FROM auditcore.evidence
                        WHERE tenant_id=:t AND di_document_id=:d AND association_status='ACTIVE'
                        """
                    ),
                    {"t": work.tenant_id, "d": document_id},
                ).scalar_one_or_none())
            stage_code = connection.execute(
                text(
                    """
                    SELECT stage_code FROM auditcore.document_capture_v2_documents
                    WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d
                    """
                ),
                {"t": work.tenant_id, "j": work.journey_id, "d": document_id},
            ).scalar_one_or_none() or "BOOKING"
        if not linked:
            return "UNLINKED"
        security_client = get_security_oauth_client()
        di_client = get_di_client()
        prefetched = _prefetch_document_for_sync(
            engine, tenant_id=work.tenant_id, journey_id=work.journey_id, document_id=document_id,
            security_client=security_client, di_client=di_client,
        )
        if prefetched is None:
            return "UNLINKED"
        _sync_booking_document_once(
            engine, tenant_id=work.tenant_id, journey_id=work.journey_id, document_id=document_id,
            service_id="p2-worker", stage_code=str(stage_code).upper(),
            security_client=security_client, di_client=di_client, prefetched=prefetched,
        )
        return "COPIED"
    except DocumentSyncLockBusyError:
        return None
    except Exception:
        logger.warning(
            "p2_page_facts_copy_failed",
            tenant_id=work.tenant_id,
            journey_id=str(work.journey_id),
            di_document_id=str(document_id),
            exc_info=True,
        )
        return None


def _reconcile_delay(oldest_submitted: datetime | None, now: datetime) -> int:
    age = (now - oldest_submitted).total_seconds() if oldest_submitted else 0.0
    if age < 60:
        return 2
    if age < 300:
        return 5
    if age < 900:
        return 15
    # Past a quarter of an hour the page is in DI's own retry ladder; one
    # look every half minute per journey is plenty and keeps a burst of
    # uploads from turning into a burst of DI listing calls. Past an hour
    # the page is queued behind a burst or waiting out a quota: five minutes.
    if age < 3600:
        return 30
    return 300


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
    requirement_refs: dict[str, str] = {}
    if pending or fragments:
        context_ref, token, _, requirement_refs = _di_context_and_requirements(engine, work)
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

    # Pages DI has finished reading: copy their values now, outside any
    # transaction, one page at a time (no lock storm), before settling.
    facts_copies: dict[str, str | None] = {}
    for page in pending:
        di_item = di_documents.get(str(page["di_document_id"]))
        if (
            di_item is not None
            and page["queue_status"] in ("CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE")
            and str(di_item.get("state") or "") == "CLASSIFIED"
            and str(di_item.get("processingStatus") or "") == "PROCESSED"
        ):
            facts_copies[str(page["queue_id"])] = _copy_page_facts(
                engine, work=work, page=dict(page), di_item=di_item, requirement_refs=requirement_refs,
            )

    now = datetime.now(UTC)
    still_waiting = False
    oldest_waiting: datetime | None = None
    facts_changed = False
    unclassified_settled = False
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
                facts_copy=facts_copies.get(str(page["queue_id"])),
            )
            if outcome.status == "READY":
                facts_changed = True
            if outcome.status == "SUPPORTING" and page["queue_status"] != "SUPPORTING":
                unclassified_settled = True
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
        if unclassified_settled:
            # A page DI could not classify needs the PC now, not at the
            # next stage recompute (which a supporting page never triggers).
            sync_unclassified_page_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
        # The file's status task ("check back after 1 hour", then "upload
        # page 2 on its own") is raised and refreshed from here: this loop
        # is what keeps ticking while pages wait on Document Intelligence.
        # It runs on the last round too: a page rejected for scan quality
        # while its siblings were still being read only becomes the High
        # task once the file is otherwise done, which is this round
        # (2026-09-30: the banner promised a task that was never raised).
        sync_processing_failure_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)

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
    # Multi-page documents, and single pages of an always-merged type (only
    # classified at page level), get one upload each: one extraction.
    read_once = read_once_di_types(registry)
    grouped = [document for document in plan if needs_document_upload(document, read_once)]

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
    facts_copy: str | None = None,
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
        facts_copy=facts_copy,
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
                last_error=COALESCE(:failure_code, last_error),
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
              -- grouping may merge the page while this reconcile runs: a
              -- merged page is final and never gets a stale DI state back
              AND queue_status <> 'MERGED'
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
            "failure_code": (di_item or {}).get("failureCode") if outcome.status == "FAILED" else None,
        },
    )

    if outcome.status in _PAGE_SETTLED_STATES and page["queue_status"] != outcome.status:
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


def _sync_rule_tasks(connection, *, tenant_id: str, journey_id: UUID, started_at: datetime | None = None) -> None:
    """Every task the document rules raise or close by themselves. One log
    line per rule that changed something, so a task's origin can be traced
    from the journey's log without opening the database."""
    outcomes = {
        "field_review": sync_field_review_tasks(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "booking_date": sync_booking_date_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "document_missing": sync_document_missing_tasks(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "name_consistency": sync_name_consistency_tasks(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "processing_failure": sync_processing_failure_tasks(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "unclassified_page": sync_unclassified_page_tasks(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "vehicle_photo": sync_vehicle_photo_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "delivery_review": sync_delivery_review_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
    }
    for rule, counts in outcomes.items():
        # The single-task rules answer with what happened to their one task.
        as_counts = {counts: 1} if isinstance(counts, str) else (counts or {})
        changed = {k: v for k, v in as_counts.items() if k != "UNCHANGED" and v}
        if changed:
            logger.info("p2_rule_tasks", tenant_id=tenant_id, journey_id=str(journey_id), rule=rule, **changed)


def settle_journey(connection, *, tenant_id: str, journey_id: UUID,
                   started_at: datetime | None = None) -> dict[str, Any]:
    """Recompute the stage from the documents, raise / close the rule tasks
    that follow from it, and recompute once more (closing tasks can pass
    the last Delivery gate). Returns the settled result with every
    transition seen on the way."""
    # The engine records STAGE_CHANGED only when the stage actually moves.
    named = sync_customer_name(connection, tenant_id=tenant_id, journey_id=journey_id)
    if named == "VERIFIED":
        logger.info("p2_customer_named", tenant_id=tenant_id, journey_id=str(journey_id), how=named)
    first = recompute_journey_stage(connection, tenant_id=tenant_id, journey_id=journey_id)
    _sync_rule_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, started_at=started_at)
    settled = recompute_journey_stage(connection, tenant_id=tenant_id, journey_id=journey_id, complete_delivery=True)
    if "DELIVERY_COMPLETED" in settled.get("transitions", ()):
        sync_delivery_review_task(connection, tenant_id=tenant_id, journey_id=journey_id,
                                  evaluation_started_at=started_at)
    # The 7th-day event: the Delivery must be complete within the window of
    # the date printed on the earliest invoice, cover note or gate pass.
    from audit_core.uc03_p2_audit_rules import schedule_delivery_completion_check

    schedule_delivery_completion_check(connection, tenant_id=tenant_id, journey_id=journey_id)
    transitions = list(dict.fromkeys([*first.get("transitions", ()), *settled.get("transitions", ())]))
    return {**settled, "transitions": transitions}


# A page still not settled this long after its last change is re-driven
# once by the nightly sweep; after that only the PC's own Retry moves it.
_NIGHTLY_SWEEP_AFTER_SECONDS = int(os.environ.get("P2_NIGHTLY_SWEEP_AFTER_SECONDS", str(24 * 60 * 60)))


def queue_nightly_upload_sweep(connection, *, tenant_id: str, now: datetime | None = None) -> dict[str, int]:
    """Every night, give every page that has been stuck for a day one more
    go, then refresh the file's status task so the PC sees the morning
    state: a failed page (other than a scan-quality rejection, which a
    retry cannot fix) is recovered through what Document Intelligence
    already holds for it (_recover_page), and sent again only if DI gave it
    up, once; a page still in flight after a day is reconciled so it settles
    one way or the other. Safe to call more than once."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=_NIGHTLY_SWEEP_AFTER_SECONDS)
    failed = connection.execute(
        text(
            """
            SELECT queue_id, journey_id, client_upload_id
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:t AND queue_status IN ('FAILED','DEAD_LETTER')
              AND updated_at_utc < :cutoff
              AND COALESCE(last_error, '') NOT LIKE 'DI_QUALITY_%'
              AND client_upload_id NOT LIKE '%~n%'
            """
        ),
        {"t": tenant_id, "cutoff": cutoff},
    ).mappings().all()
    journeys: set[UUID] = set()
    for row in failed:
        journey_id = UUID(str(row["journey_id"]))
        request_page_recovery(
            connection, tenant_id=tenant_id, journey_id=journey_id, queue_id=UUID(str(row["queue_id"])),
            marker="n", requested_by="SYSTEM", correlation_id=None,
        )
        record_activity(
            connection, tenant_id=tenant_id, journey_id=journey_id, event_type="PAGE_RETRY_REQUESTED",
            subject_type="DOCUMENT_PAGE", subject_id=str(row["queue_id"]), details={"by": "nightly sweep"},
            correlation_id=None,
        )
        journeys.add(journey_id)
    stuck = connection.execute(
        text(
            """
            SELECT DISTINCT journey_id
            FROM auditcore.p2_document_queue
            WHERE tenant_id=:t AND queue_status = ANY(:states) AND updated_at_utc < :cutoff
            """
        ),
        {"t": tenant_id, "states": list(_PAGE_ACTIVE_STATES), "cutoff": cutoff},
    ).scalars().all()
    # The copy safety net (2026-09-30): a page DI has read but Audit Core
    # holds no values for is put back in line for one copy (a read of what
    # DI already holds, not a re-upload): a page the old timer-based path
    # marked "nothing read" without the worker ever copying DI's result, or
    # a page once READY whose values are gone. A page the worker itself
    # read as empty carries _NOTHING_READ_REASON and is left alone.
    recopy = connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue AS q
            SET queue_status='SYNCING_TO_AUDIT_CORE', status_reason=NULL, updated_at_utc=now()
            WHERE q.tenant_id=:t AND q.di_document_id IS NOT NULL
              AND q.updated_at_utc < :cutoff
              AND (
                (q.queue_status='NEEDS_REVIEW' AND q.status_reason IS DISTINCT FROM :reason)
                OR (q.queue_status='READY' AND NOT EXISTS (
                    SELECT 1 FROM auditcore.journey_document_extracted_fields AS f
                    WHERE f.tenant_id=q.tenant_id AND f.journey_id=q.journey_id
                      AND f.di_document_id=q.di_document_id
                ))
              )
            RETURNING q.journey_id
            """
        ),
        {"t": tenant_id, "cutoff": cutoff, "reason": _NOTHING_READ_REASON},
    ).scalars().all()
    stuck = list(dict.fromkeys([*stuck, *recopy]))
    for journey_id in stuck:
        journey_id = UUID(str(journey_id))
        enqueue_work(
            connection, tenant_id=tenant_id, journey_id=journey_id, work_type="JOURNEY_RECONCILE",
            work_key=str(journey_id), payload={"uploadedBy": "SYSTEM", "uploadedByRole": "PC"},
            correlation_id=None,
        )
        journeys.add(journey_id)
    for journey_id in journeys:
        sync_processing_failure_tasks(connection, tenant_id=tenant_id, journey_id=journey_id)
    return {"retried": len(failed), "reconciled": len(stuck), "journeys": len(journeys)}


def _stage_recompute(engine: Engine, work: WorkItem) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        _owned(connection, work, lock=False)
        result = settle_journey(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
        # Delivery documents just became complete: every compliance rule runs
        # now (forced), not at a button press.
        force = "DELIVERY_DOCUMENTS_COMPLETE" in result.get("transitions", ())
        request_control_evaluation(
            connection,
            tenant_id=work.tenant_id,
            journey_id=work.journey_id,
            correlation_id=work.correlation_id,
            delay_seconds=0 if force else 3,
            force=force,
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
            sync_booking_date_task(
                connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                evaluation_started_at=datetime.now(UTC),
            )
            return
        if source_type in {"EVIDENCE", "DOCUMENT", "REVIEW"}:
            if source_type == "REVIEW":
                reviewer = connection.execute(
                    text(
                        """
                        SELECT actor_id, actor_role_code FROM auditcore.p2_task_events
                        WHERE tenant_id=:t AND task_id=:task AND event_type='COMPLETE_ACTION'
                        ORDER BY created_at_utc DESC LIMIT 1
                        """
                    ),
                    {"t": work.tenant_id, "task": task_id},
                ).mappings().one_or_none()
                mark_delivery_reviewed(
                    connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                    actor_id=reviewer["actor_id"] if reviewer else None,
                    actor_role=reviewer["actor_role_code"] if reviewer else None,
                    correlation_id=work.correlation_id,
                )
            # Fresh facts first, then the producer closes or returns the task.
            result = settle_journey(connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                                    started_at=datetime.now(UTC))
            if "DELIVERY_DOCUMENTS_COMPLETE" in result["transitions"]:
                request_control_evaluation(
                    connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                    correlation_id=work.correlation_id, delay_seconds=0, force=True,
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
_last_nightly_review: dict[str, str] = {}


def _nightly_review_due(tenant_id: str, now: datetime | None = None) -> str | None:
    """The date whose nightly review this worker still owes, once the
    configured UTC time has passed; None otherwise."""
    now = now or datetime.now(UTC)
    hour, _, minute = _NIGHTLY_REVIEW_UTC.partition(":")
    due_at = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
    if now < due_at:
        return None
    today = now.date().isoformat()
    return None if _last_nightly_review.get(tenant_id) == today else today


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
    due = _nightly_review_due(tenant_id)
    if due is None:
        return
    # Re-run the Delivery checks for every delivery in progress: the
    # time-based checks fire without a document event. Idempotent per night.
    from audit_core.uc03_p2_audit_rules import queue_nightly_review

    try:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            queued = queue_nightly_review(connection, tenant_id=tenant_id)
            swept = queue_nightly_upload_sweep(connection, tenant_id=tenant_id)
        _last_nightly_review[tenant_id] = due
        logger.info("p2_nightly_review_queued", tenant_id=tenant_id, night=due, journeys=queued, **swept)
    except Exception:
        logger.warning("p2_nightly_review_failed", tenant_id=tenant_id, exc_info=True)


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

    renewing = threading.Event()
    renewer = threading.Thread(target=_keep_leases, args=(engine, claimed, renewing), daemon=True)
    renewer.start()
    try:
        _run_claimed(engine, claimed)
    finally:
        renewing.set()
        renewer.join(timeout=5)
    _stats["processed"] += len(claimed)
    return len(claimed)


def _keep_leases(engine: Engine, items: list[WorkItem], done: threading.Event) -> None:
    """Extend the lease of items still running, so a long split or a slow DI call is not
    reclaimed (and processed twice) by another worker. Stops when the batch settles."""
    while not done.wait(_LEASE_RENEW_SECONDS):
        running = [item for item in items if item.work_id in _started_at]
        for tenant_id in {item.tenant_id for item in running}:
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    for item in (i for i in running if i.tenant_id == tenant_id):
                        connection.execute(
                            text(
                                """
                                UPDATE auditcore.p2_work_queue
                                SET lease_expires_at_utc=now() + (:lease_seconds * interval '1 second')
                                WHERE tenant_id=:tenant_id AND work_id=:work_id AND lease_token=:token
                                """
                            ),
                            {"tenant_id": tenant_id, "work_id": item.work_id, "token": item.lease_token,
                             "lease_seconds": _LEASE_SECONDS},
                        )
            except Exception as exc:  # noqa: BLE001 - renewal is best effort; expiry is the fallback
                logger.warning("p2_work_lease_renewal_failed", tenant_id=tenant_id, **exception_summary(exc))


def _run_claimed(engine: Engine, claimed: list[WorkItem]) -> None:
    with ThreadPoolExecutor(max_workers=_WORKER_CONCURRENCY) as pool:
        futures = {pool.submit(_process_in_context, engine, item): item for item in claimed}
        for future in as_completed(futures):
            work = futures[future]
            started = _started_at.pop(work.work_id, None)
            duration_ms = round((time.perf_counter() - started) * 1000.0, 1) if started else None
            try:
                future.result()
            except RescheduleWork as exc:
                logger.info("p2_work_rescheduled", retry_in_seconds=exc.delay_seconds,
                            reason=redact_text(str(exc), 200), duration_ms=duration_ms, **_work_fields(work))
                _settle(engine, work, _reschedule, exc)
            except LeaseLost:
                logger.warning("p2_work_lease_lost", duration_ms=duration_ms, **_work_fields(work))
            except Exception as exc:  # noqa: BLE001 - every failure is settled (retry/dead-letter)
                # _fail logs p2_work_retry_scheduled / p2_work_dead_lettered with the summary.
                _settle(engine, work, _fail, exc)
            else:
                logger.info("p2_work_completed", duration_ms=duration_ms, **_work_fields(work))
                _settle(engine, work, _complete)


_started_at: dict[UUID, float] = {}
_stats = {"processed": 0}


def _process_in_context(engine: Engine, work: WorkItem) -> None:
    """Run one item with its identifiers bound to every log line it produces (pool threads do
    not inherit context) and its correlation id sent on every outbound call."""
    _started_at[work.work_id] = time.perf_counter()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**_work_fields(work))
    try:
        process_work(engine, work)
    finally:
        structlog.contextvars.clear_contextvars()


_HEARTBEAT_SECONDS = 300


def _queue_depth(engine: Engine) -> dict[str, Any]:
    """Waiting / running / dead-lettered items and the oldest waiting item's age, across
    tenants: a growing backlog or an old waiting item means the worker is not keeping up."""
    totals = {"queue_waiting": 0, "queue_running": 0, "queue_dead_letter_24h": 0}
    oldest: float | None = None
    try:
        for tenant_id in _active_tenants(engine):
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                row = connection.execute(
                    text(
                        """
                        SELECT
                          count(*) FILTER (WHERE work_status IN ('PENDING','RETRY_WAIT')) AS waiting,
                          count(*) FILTER (WHERE work_status IN ('CLAIMED','PROCESSING')) AS running,
                          count(*) FILTER (WHERE work_status='DEAD_LETTER'
                                             AND updated_at_utc > now() - interval '24 hours') AS dead,
                          EXTRACT(EPOCH FROM now() - min(created_at_utc)
                                  FILTER (WHERE work_status IN ('PENDING','RETRY_WAIT')
                                          AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc <= now())))
                            AS oldest_seconds
                        FROM auditcore.p2_work_queue
                        WHERE tenant_id=:tenant_id
                        """
                    ),
                    {"tenant_id": tenant_id},
                ).mappings().one()
            totals["queue_waiting"] += int(row["waiting"])
            totals["queue_running"] += int(row["running"])
            totals["queue_dead_letter_24h"] += int(row["dead"])
            if row["oldest_seconds"] is not None:
                oldest = max(oldest or 0.0, float(row["oldest_seconds"]))
    except Exception as exc:  # noqa: BLE001 - the heartbeat must never stop the worker
        return {"queue_depth_error": exception_summary(exc)["exc_type"]}
    return {**totals, "queue_oldest_ready_seconds": round(oldest) if oldest is not None else None}


def _configure_worker_observability() -> None:
    try:
        configure_logging(load_settings(), process="p2-worker")
    except SettingsError:
        # Never let logging configuration stop the worker; structlog defaults still print.
        logger.warning("p2_worker_logging_not_configured", reason_code="SETTINGS_INCOMPLETE")
    install_correlation_propagation()


def main() -> None:
    _configure_worker_observability()
    engine = get_engine()
    logger.info(
        "p2_worker_started",
        worker_id=_WORKER_ID,
        concurrency=_WORKER_CONCURRENCY,
        per_journey_concurrency=_PER_JOURNEY_CONCURRENCY,
    )
    last_heartbeat = time.monotonic()
    cycles = failed_cycles = 0
    while True:
        cycles += 1
        try:
            processed = run_once(engine)
        except Exception as exc:  # noqa: BLE001 - logged; the loop must survive
            # A database blip must not kill the worker process.
            failed_cycles += 1
            logger.error("p2_worker_cycle_failed", error_category="TECHNICAL", **exception_summary(exc))
            processed = 0
        if time.monotonic() - last_heartbeat >= _HEARTBEAT_SECONDS:
            # Proof of life for a quiet worker; a missing heartbeat means it is stuck or down.
            logger.info("p2_worker_heartbeat", worker_id=_WORKER_ID, cycles=cycles,
                        failed_cycles=failed_cycles, processed=_stats["processed"], **_queue_depth(engine))
            last_heartbeat = time.monotonic()
            cycles = failed_cycles = 0
            _stats["processed"] = 0
        if processed == 0:
            time.sleep(_POLL_SECONDS)


if __name__ == "__main__":
    main()
