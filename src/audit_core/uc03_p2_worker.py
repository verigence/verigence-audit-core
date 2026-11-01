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
import signal
import socket
import threading
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
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

from audit_core import uc03_p2_runtime as _runtime
from audit_core.config import SettingsError, load_settings
from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import get_engine
from audit_core.di_capture_v2_client import DiCaptureV2Error
from audit_core.logging_config import configure_logging, exception_summary, redact_text
from audit_core.otel import install_correlation_propagation
from audit_core.platform_health import start_heartbeat_thread
from audit_core.uc03_db_transient import work_item_scope
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
from audit_core.uc03_insurance_source import sync_insurance_source_task
from audit_core.uc03_p2_controls import (
    evaluate_unit,
    mark_unit_controls,
    request_control_evaluation,
)
from audit_core.uc03_p2_customer import follow_entered_name, sync_customer_name
from audit_core.uc03_p2_grouping import (
    PageFact,
    merge_pdf_pages,
    needs_document_upload,
    plan_documents,
    read_once_di_types,
)
from audit_core.uc03_p2_object_keys import root_of
from audit_core.uc03_p2_price_variance import (
    backfill_price_variance,
    refresh_price_variance,
)
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_retry_stop import (
    STOPPED_CODE,
    STOPPED_EVENT,
    automatic_retry_stop_reason,
    stop_reason_text,
    stopped_details,
)
from audit_core.uc03_p2_runtime import (
    DAYEND_SWEEP_DEFAULT_UTC,
    claim_scheduled_run,
    enqueue_work,
    fact_fingerprint,
    hhmm,
    mark_derived_writes,
    note_facts_changed,
    record_activity,
    request_page_reread,
    requeue_page_for_ingest,
    swept_pending_sql,
)
from audit_core.uc03_p2_stage import recompute_journey_stage
from audit_core.uc03_p2_storage import get_p2_document_storage
from audit_core.uc03_p2_task_producer import (
    FILE_NOT_SPLIT_IN_TIME_REASON,
    FILE_NOT_UPLOADED_IN_TIME_REASON,
    SETUP_FAILURE_CODE,
    SETUP_PAGE_TEXT,
    apply_control_transitions,
    raise_or_refresh,
    sync_booking_date_task,
    sync_dead_work_tasks,
    sync_delivery_review_task,
    sync_document_missing_tasks,
    sync_field_review_tasks,
    sync_large_upload_tasks,
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
# storage down, rate limited): spread out so a surge never piles retries
# onto itself (decision 2026-10-01: 15, 30, 60 and 120 minutes), then the
# PC is told. A fault that cannot recover (the document service
# refused the request, an unreadable file) is told at once.
_RETRY_DELAYS_SECONDS = (900, 1800, 3600, 7200)
_MAX_ATTEMPTS = int(os.environ.get("P2_WORKER_MAX_ATTEMPTS", str(len(_RETRY_DELAYS_SECONDS) + 1)))
# A document's own processing steps -- splitting the upload into pages,
# sending a page to Document Intelligence, asking DI to read a page again,
# sending a merged document -- follow the product owner's rule of
# 2026-10-21 instead: a technical failure (the document service or storage
# not answering or answering with an error, a timeout) is tried at T0, again
# at T0+10 and T0+20 minutes, and then the page FAILS and the PC gets the
# "Document to resubmit" task. No file takes more than 30 minutes
# (fail_unsettled_pages).
_DOCUMENT_WORK_TYPES = frozenset({"DOCUMENT_INGEST", "SPLIT_BATCH", "PAGE_REREAD", "BATCH_GROUP"})
_DOCUMENT_RETRY_DELAYS_SECONDS = (600, 600)
_DOCUMENT_MAX_ATTEMPTS = len(_DOCUMENT_RETRY_DELAYS_SECONDS) + 1
# Polling DI for results (the journey reconcile) never stretches to hours on
# a few DI timeouts (2026-10-20 review): 1, 2, then every 5 minutes, and a
# poll that works resets the count (_reschedule). After an hour of failures
# in a row the step stops and the Team Lead is told (_mark_dead).
_POLL_WORK_TYPES = frozenset({"JOURNEY_RECONCILE", "DOCUMENT_RECONCILE"})
# A page failed on the clock, or uploaded again, has its DI document deleted
# (review of #469, 2026-10-21): DI stops reading it, nothing is read twice.
WITHDRAW_WORK_TYPE = "DI_DOCUMENT_WITHDRAW"
# The same file uploaded again is deleted by the system (2026-10-31, uc03_p2_same_file).
SAME_FILE_DELETE_WORK_TYPE = "SAME_FILE_DELETE"
_POLL_RETRY_DELAYS_SECONDS = (60, 120, 300)
_POLL_MAX_ATTEMPTS = 13


def _ladder(work_type: str, payload: dict[str, Any] | None = None) -> tuple[tuple[int, ...], int]:
    # Deleting a given-up page at DI is retried like a poll: soon, then
    # every 5 minutes, for about an hour (then the Team Lead is told).
    # Deleting the same file uploaded again (2026-10-31) is retried the same
    # way: Document Intelligence or R2 not answering is soon over.
    if work_type in _POLL_WORK_TYPES or work_type in (WITHDRAW_WORK_TYPE, SAME_FILE_DELETE_WORK_TYPE):
        return _POLL_RETRY_DELAYS_SECONDS, _POLL_MAX_ATTEMPTS
    if work_type in _DOCUMENT_WORK_TYPES:
        return _DOCUMENT_RETRY_DELAYS_SECONDS, _DOCUMENT_MAX_ATTEMPTS
    # Background recomputation (stage, task verification, controls) keeps
    # the spread-out ladder of 2026-10-01: the PC does not wait on it.
    return _RETRY_DELAYS_SECONDS, _MAX_ATTEMPTS


def _max_attempts(work_type: str, payload: dict[str, Any] | None = None) -> int:
    return _ladder(work_type, payload)[1]


def _retry_delay(work_type: str, attempts: int, payload: dict[str, Any] | None = None) -> int:
    """Seconds to wait after failed attempt ``attempts`` (numbered from 1)."""
    delays = _ladder(work_type, payload)[0]
    return delays[min(attempts, len(delays)) - 1]
_POLL_SECONDS = float(os.environ.get("P2_WORKER_POLL_SECONDS", "1.0"))
_MAX_PDF_PAGES = int(os.environ.get("P2_MAX_PDF_PAGES", "100"))
# 10 parallel items (2026-10-27, was 6): the container has 8 vCPU and the DB pool 30.
_WORKER_CONCURRENCY = max(1, int(os.environ.get("P2_WORKER_CONCURRENCY", "10")))
# Page work (split, read, re-read, group, withdraw) of one Journey side by side
# (2026-10-29: 6, was 3): it mostly waits on Document Intelligence, and a 20-page
# upload read three at a time waited behind itself. Journey work runs alone.
_PER_JOURNEY_CONCURRENCY = max(1, int(os.environ.get("P2_PER_JOURNEY_CONCURRENCY", "6")))
_LEASE_SECONDS = int(os.environ.get("P2_WORKER_LEASE_SECONDS", "600"))
_LEASE_RENEW_SECONDS = max(5, _LEASE_SECONDS // 3)
# While Document Intelligence still has a page in hand it is polled
# (_reconcile_delay); DI's own verdict, or DI no longer knowing the page,
# settles it. Product owner 2026-10-21 (replacing "load is never a failure"
# of 2026-09-30): a page still not settled 30 minutes after upload is failed
# (fail_unsettled_pages) and the PC resubmits it.
# A page the PC asked to recover from FAILED is looked
# up at DI first (_recover_page); only a PC's Retry ever sends it again.
# DI reports PROCESSED before the document-link sync has copied facts into
# Audit Core. Allow the sync this long before treating "no facts" as a result.
_FACT_SWEEP_SECONDS = float(os.environ.get("P2_FACT_SWEEP_SECONDS", "30"))
_FACT_SWEEP_WINDOW_MINUTES = int(os.environ.get("P2_FACT_SWEEP_WINDOW_MINUTES", "10"))
# When (UTC, HH:MM) the nightly review of deliveries in progress is queued.
# (A delivery review, not document processing: left as it was on 2026-10-22.)
_NIGHTLY_REVIEW_UTC = os.environ.get("P2_NIGHTLY_REVIEW_UTC", "20:30")


# When (UTC, HH:MM) the day-end sweep clears the pages that ended FAILED from
# their Journeys: 02:00 IST = 20:30 UTC by default (product owner 2026-10-22);
# the one setting lives in uc03_p2_runtime because the PC's messages quote it.
_hhmm = hhmm
_DAYEND_SWEEP_DEFAULT_UTC = DAYEND_SWEEP_DEFAULT_UTC
# A worker that was down at the time still runs the missed sweep when it is
# back, for this long after the time; later, that night is skipped.
_DAYEND_CATCHUP = timedelta(hours=6)
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


class JourneyDeleted(RuntimeError):
    """The item deleted its own Journey (the same file uploaded again): its
    queue row went with the Journey, so there is nothing left to settle."""


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


# Work on one Journey (2026-10-28). A page's own work (split, read, re-read,
# group, withdraw) may run up to _PER_JOURNEY_CONCURRENCY at a time: it waits on
# Document Intelligence and touches its own page. Work on the Journey as a whole
# -- control checks, reconcile, stage, task verification -- runs alone: two of
# them at once updated the same Deal, stage-flag, payment and product rows in a
# different order and Postgres killed one (881 deadlocks; the killed step was
# skipped and the item still reported success). A Journey item starts only
# when nothing else of its Journey is running and it is the oldest waiting;
# page work queued after it waits for it.
PAGE_WORK_TYPES = ("SPLIT_BATCH", "DOCUMENT_INGEST", "PAGE_REREAD", "BATCH_GROUP", "DI_DOCUMENT_WITHDRAW")
# How long a deletion that has started holds its Journey's work back. A
# deletion takes seconds; one left STARTED by a crash stops holding it then.
DELETION_HOLD_MINUTES = 15

_CLAIM_SQL = text(
    """
    WITH inflight AS (
        SELECT journey_id, COUNT(*) AS running,
               COUNT(*) FILTER (WHERE work_type <> ALL(:page_types)) AS running_journey_work
        FROM auditcore.p2_work_queue
        WHERE tenant_id=:tenant_id
          AND work_status IN ('CLAIMED','PROCESSING')
          AND lease_expires_at_utc > now()
        GROUP BY journey_id
    ),
    ready AS (
        SELECT work_id, journey_id, queued_at_utc, (work_type = ANY(:page_types)) AS page_work,
               row_number() OVER (PARTITION BY journey_id ORDER BY queued_at_utc, work_id) AS journey_rank,
               -- Among a Journey's own work its checks come last (2026-10-31):
               -- they read what the recompute, the reconcile and task
               -- verification settle, and a recompute asked for again now
               -- joins the back of the line (migration 0180) -- ahead of it,
               -- a check left from the previous change would run on unsettled
               -- facts and then once more after the recompute. The derived
               -- unit comes after the other checks: it reads their execution
               -- log, so run first it ran again on the next request. Pages
               -- keep their place by queue time.
               row_number() OVER (PARTITION BY journey_id, (work_type = ANY(:page_types))
                                  ORDER BY (work_type = 'CONTROL_EVALUATE'), (work_key LIKE '%:P2:DERIVED'),
                                           queued_at_utc, work_id)
                 AS journey_work_rank
        FROM auditcore.p2_work_queue
        WHERE tenant_id=:tenant_id
          AND (
            work_status IN ('PENDING','RETRY_WAIT')
            OR (work_status IN ('CLAIMED','PROCESSING') AND lease_expires_at_utc <= now())
          )
          AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc <= now())
          -- A Journey being deleted gets no new work (2026-10-31): a check
          -- still writing its findings while the delete removed its rows
          -- deadlocked with it (the delete takes the audit tables' trigger
          -- locks). The delete waits for work already running
          -- (uc03_p2_journey_delete._wait_for_journey_work); only the
          -- delete's own item runs.
          AND (work_type = :same_file_delete OR NOT EXISTS (
                SELECT 1 FROM auditcore.p2_journey_deletions d
                WHERE d.tenant_id=:tenant_id AND d.journey_id=p2_work_queue.journey_id
                  AND d.deletion_status='STARTED'
                  AND d.requested_at_utc > now() - (:deletion_hold_minutes * interval '1 minute')))
    ),
    first_journey_work AS (
        SELECT journey_id, MIN(journey_rank) AS journey_rank FROM ready WHERE NOT page_work GROUP BY journey_id
    ),
    eligible AS (
        SELECT r.work_id, r.page_work, r.queued_at_utc
        FROM ready r
        LEFT JOIN inflight i ON i.journey_id=r.journey_id
        LEFT JOIN first_journey_work f ON f.journey_id=r.journey_id
        WHERE CASE
          WHEN r.page_work THEN
            COALESCE(i.running_journey_work, 0) = 0
            AND r.journey_rank + COALESCE(i.running, 0) <= :per_journey
            AND r.journey_rank < COALESCE(f.journey_rank, 2147483647)
          ELSE COALESCE(i.running, 0) = 0 AND f.journey_rank = 1 AND r.journey_work_rank = 1
        END
        -- Reading first (2026-10-29): 383 of 554 pages failed by the 30-minute
        -- rule were never sent to Document Intelligence -- they waited behind
        -- control checks and recomputes. A page's work is claimed before any
        -- evaluation work, oldest first within each class.
        ORDER BY (NOT r.page_work), r.queued_at_utc, r.work_id
        LIMIT :limit
    )
    SELECT w.tenant_id, w.work_id, w.journey_id, w.work_type, w.work_key,
           w.payload, w.attempt_count, w.requested_version, w.correlation_id,
           (w.work_status IN ('CLAIMED','PROCESSING')) AS reclaimed
    FROM auditcore.p2_work_queue w
    JOIN eligible e ON e.work_id=w.work_id
    WHERE w.tenant_id=:tenant_id
    ORDER BY (NOT e.page_work), w.queued_at_utc, w.work_id
    FOR UPDATE OF w SKIP LOCKED
    """
)


def _claim_for_tenant(engine: Engine, tenant_id: str, limit: int) -> list[WorkItem]:
    claimed: list[WorkItem] = []
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(
            _CLAIM_SQL,
            {"tenant_id": tenant_id, "limit": limit, "per_journey": _PER_JOURNEY_CONCURRENCY,
             "page_types": list(PAGE_WORK_TYPES), "same_file_delete": SAME_FILE_DELETE_WORK_TYPE,
             "deletion_hold_minutes": DELETION_HOLD_MINUTES},
        ).mappings().all()
        for row in rows:
            token = uuid4()
            # A reclaimed expired lease means the previous worker died while
            # holding the item: that counts as a failed attempt, so a crash-
            # looping item eventually dead-letters instead of cycling forever.
            attempts = int(row["attempt_count"] or 0) + (1 if row["reclaimed"] else 0)
            item = _work_item(row, attempts=attempts, token=token)
            if row["reclaimed"] and attempts >= _max_attempts(str(row["work_type"]), dict(row["payload"] or {})):
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
                    -- asked for again while it ran: it joins the back of the line (2026-10-28)
                    queued_at_utc=CASE WHEN CAST(:status AS varchar)='PENDING' THEN now() ELSE queued_at_utc END,
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
        if not stale_after_run:
            # A step that stopped earlier ran again: its task closes itself
            # (2026-10-20 re-review, R2).
            sync_dead_work_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)


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
                    -- the step ran and only has to wait: earlier failures no
                    -- longer count toward its limit (2026-10-20 review)
                    attempt_count=0,
                    -- it ran: when it is due again it joins the back of the line (2026-10-28)
                    queued_at_utc=now(),
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


def _technical_cause(exc: Exception) -> str:
    """What failed, in the PC's words, for a fault that trying again can fix
    (product owner's rule 2026-10-20: say which case it is)."""
    if isinstance(exc, TransportError) or (
        isinstance(exc, DiCaptureV2Error) and str(exc.detail).startswith("DI V2 request failed")
    ):
        return "Document service didn't respond"
    if isinstance(exc, DiCaptureV2Error):
        return "Document service returned an error"
    if isinstance(exc, HTTPStatusError):
        return "Document storage returned an error"
    return "A system problem stopped this page"


_ATTEMPT_WORDS = {1: "once", 2: "twice", 3: "3 times (at once, then 10 and 20 minutes later)"}


def _fail(engine: Engine, work: WorkItem, exc: Exception) -> None:
    attempts = work.attempt_count + 1
    retryable = _is_retryable(exc)
    terminal = (not retryable) or attempts >= _max_attempts(work.work_type, work.payload)
    delay = _retry_delay(work.work_type, attempts, work.payload)
    # A technical fault on a document's own step is told as such once its
    # three attempts (T0, +10, +20 minutes) are spent (2026-10-21); a
    # refusal keeps its own reason.
    cause = (
        f"{_technical_cause(exc)} — tried {_ATTEMPT_WORDS.get(attempts, f'{attempts} times')}."
        if retryable and work.work_type in _DOCUMENT_WORK_TYPES
        else _plain_cause(exc)
    )
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
        if not terminal and work.work_type in _PAGE_RETRY_WORK_TYPES:
            # Product owner 2026-10-27: once the PC has closed the page's
            # task, no automatic retry (uc03_p2_retry_stop); while it is
            # open, the T0 / +10 / +20 minute retries run as before.
            why = automatic_retry_stop_reason(connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                                              queue_id=work.work_key)
            if why:
                _stop_automatic_retry(connection, work, why=why, error=error, attempts=attempts, cancel_work=True)
                return
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
                    reason=(f"{_technical_cause(exc)} — trying again in {_delay_text(delay)} "
                            f"(attempt {attempts} of {_max_attempts(work.work_type)}). Nothing to do yet."),
                    last_error=error,
                )
                # The file's task turns into "Document to resubmit" if the
                # last attempt fails too, and closes itself if it works.
                sync_processing_failure_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
            return
        _mark_dead(connection, work, attempts=attempts, retryable=retryable, cause=cause, error=error)


# The page steps whose automatic retries stop once the PC closed the task.
_PAGE_RETRY_WORK_TYPES = frozenset({"DOCUMENT_INGEST", "PAGE_REREAD"})


def _stop_automatic_retry(
    connection, work: WorkItem, *, why: str, error: str | None, attempts: int | None, cancel_work: bool,
) -> None:
    """Do not try this page again by itself (uc03_p2_retry_stop). The work
    item is cancelled (``cancel_work``; otherwise the caller completes it),
    a page still in hand is failed with the reason -- a page already settled
    (kept as Others, removed, failed) is left as it is -- and the decision is
    recorded. The file's tasks are refreshed as for any failed page."""
    logger.info("p2_work_retry_stopped", why=why, **_work_fields(work))
    if cancel_work:
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_work_queue
                SET work_status='CANCELLED', attempt_count=COALESCE(:attempts, attempt_count),
                    next_attempt_at_utc=NULL, lease_token=NULL, lease_expires_at_utc=NULL,
                    last_error=:error, updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND work_id=:work_id
                """
            ),
            {"tenant_id": work.tenant_id, "work_id": work.work_id, "attempts": attempts,
             "error": (error or f"{STOPPED_CODE}: {why}")[:1800]},
        )
    batch_id = connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status='FAILED', status_reason=:reason, last_error=:code,
                next_attempt_at_utc=NULL, updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND queue_id=CAST(:queue_id AS uuid) AND queue_status = ANY(:active)
            RETURNING batch_id
            """
        ),
        {"tenant_id": work.tenant_id, "queue_id": work.work_key, "reason": stop_reason_text(why),
         "code": STOPPED_CODE, "active": list(_PAGE_ACTIVE_STATES)},
    ).scalar_one_or_none()
    if batch_id is not None:
        _refresh_batch_status(connection, work.tenant_id, UUID(str(batch_id)))
    record_activity(
        connection, tenant_id=work.tenant_id, journey_id=work.journey_id, event_type=STOPPED_EVENT,
        subject_type="DOCUMENT_PAGE", subject_id=work.work_key,
        details=stopped_details(work.work_type, work.work_key, why,
                                {"pageFailed": batch_id is not None, "attempts": attempts, "error": error}),
        correlation_id=work.correlation_id,
    )
    if batch_id is not None:
        sync_processing_failure_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)


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
    """Terminal outcome for a work item: settle what it was working on, tell the PC
    (or, for a background step, the Team Lead) and record the activity. No dead
    letter is silent (2026-10-20 review)."""
    if work.work_type == "PAGE_REREAD":
        _settle_page(
            connection,
            tenant_id=work.tenant_id,
            queue_id=UUID(work.work_key),
            status="FAILED",
            reason=f"{cause} \"Read again\" could not be sent. Retry the page from the card, or upload it again.",
            last_error=error,
        )
        sync_processing_failure_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
    elif work.work_type == "BATCH_GROUP":
        # Grouping stopped (review of #469, 2026-10-21): the pages waiting to
        # be put together fail, and the file's "Document to resubmit" task
        # names them -- never a task of its own kind.
        for page in _fail_grouping(connection, tenant_id=work.tenant_id, batch_id=UUID(work.work_key),
                                   why="BATCH_GROUP_DEAD"):
            withdraw_at_di(connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                           queue_id=page["queue_id"], di_document_id=page["di_document_id"],
                           why=GROUPING_FAILED_CODE)
        sync_processing_failure_tasks(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
    elif work.work_type == "DOCUMENT_INGEST":
        _settle_page(
            connection,
            tenant_id=work.tenant_id,
            queue_id=UUID(work.work_key),
            status="FAILED",
            reason=(f"{cause} " + ("Retry the page from the card, or upload it again."
                                   if retryable else "Retry the page, remove the upload, or delete this booking.")),
            last_error=error,
            attempts=attempts,
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
    elif work.work_type == SAME_FILE_DELETE_WORK_TYPE:
        # The alert still goes out, saying the file could not be deleted; the
        # Team Lead's task below says which step stopped.
        from audit_core.uc03_p2_same_file import DELETE_FAILED, publish_alert

        publish_alert(connection, tenant_id=work.tenant_id, journey_id=work.journey_id, outcome=DELETE_FAILED)
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
    if work.work_type not in ("DOCUMENT_INGEST", "SPLIT_BATCH", "PAGE_REREAD", "BATCH_GROUP"):
        # A background step (checking DI for results, recomputing the stage,
        # verifying tasks, evaluating controls) stopped: the Team Lead is told.
        label = _BACKGROUND_STEP_WORDS.get(work.work_type, work.work_type.replace("_", " ").lower())
        _raise_dead_work_task(
            connection, work, task_type="TL_DOCUMENT_STUCK",
            dedupe_key=f"work-dead:{work.journey_id}:{work.work_type}",
            title=f"Background step stopped: {label}",
            description=(f"Audit Core stopped {label} for this booking after {attempts} failed "
                         f"attempt{'' if attempts == 1 else 's'}. {_BACKGROUND_STEP_EFFECT.get(work.work_type, '')}"
                         "Use Re-sync on Upload / Edit Documents to start it again; if it stops again, "
                         "raise it with support (the error is recorded on the work item)."),
            error=error,
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
            "error": error,
        },
        correlation_id=work.correlation_id,
    )


_BACKGROUND_STEP_WORDS = {
    "JOURNEY_RECONCILE": "checking the document service for this booking's pages",
    "DOCUMENT_RECONCILE": "checking the document service for this booking's pages",
    "STAGE_RECOMPUTE": "recomputing this booking's stage",
    "TASK_VERIFY": "verifying this booking's tasks",
    "CONTROL_EVALUATE": "evaluating this booking's checks",
    "DI_DOCUMENT_WITHDRAW": "withdrawing a failed or replaced page from the document service",
    SAME_FILE_DELETE_WORK_TYPE: "deleting this booking (the same file as an earlier one)",
}
_BACKGROUND_STEP_EFFECT = {
    "JOURNEY_RECONCILE": "Pages may show as still being read although the document service has finished. ",
    "DOCUMENT_RECONCILE": "Pages may show as still being read although the document service has finished. ",
    "STAGE_RECOMPUTE": "The booking's stage may be out of date. ",
    "CONTROL_EVALUATE": "Some checks may show as not evaluated. ",
    "DI_DOCUMENT_WITHDRAW": "The document service may still be reading a page Audit Core has already failed or replaced. ",
    SAME_FILE_DELETE_WORK_TYPE: ("This booking is the same file as an earlier one (PAN, mobile, customer name "
                                 "and model) and is still on file; delete it from the booking. "),
}


def _raise_dead_work_task(
    connection, work: WorkItem, *, task_type: str, dedupe_key: str, title: str, description: str, error: str,
) -> None:
    """The task that says a step stopped for good (2026-10-20 review: no dead
    letter is silent). Logged and skipped if the task cannot be raised, so
    the dead letter itself is still recorded."""
    try:
        with connection.begin_nested():
            raise_or_refresh(
                connection, tenant_id=work.tenant_id, journey_id=work.journey_id, dedupe_key=dedupe_key,
                task_type=task_type, source_type="DOCUMENT", source_code="P2_WORK_DEAD_LETTER",
                title=title, description=description,
                reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT", "sourceCode": "P2_WORK_DEAD_LETTER",
                           "workType": work.work_type, "workKey": work.work_key, "error": error[:300]},
                severity="HIGH", registry=get_registry(),
            )
    except Exception:  # the dead letter must still be recorded
        logger.warning("p2_dead_letter_task_not_raised", **_work_fields(work), exc_info=True)


_enqueue = enqueue_work


def _settle_page(
    connection,
    *,
    tenant_id: str,
    queue_id: UUID,
    status: str,
    reason: str | None,
    last_error: str | None = None,
    attempts: int | None = None,
) -> UUID | None:
    batch_id = connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status=:status,
                status_reason=:reason,
                last_error=COALESCE(:last_error, last_error),
                -- the attempts made, for the failure report (2026-10-21)
                attempt_count=COALESCE(:attempts, attempt_count),
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
              -- running work never writes over a page settled meanwhile:
              -- merged, removed, or failed (by the 30-minute clock among
              -- others; review of #469, 2026-10-21)
              AND queue_status NOT IN ('MERGED','FAILED','DEAD_LETTER','CANCELLED')
            RETURNING batch_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "queue_id": queue_id,
            "status": status,
            "reason": reason,
            "last_error": last_error,
            "attempts": attempts,
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
    which earlier upload it repeats. True when refused. A file some of whose
    pages failed may be uploaded again whole (the PC's "Document to
    resubmit", 2026-10-21): its failed pages are replaced by the fresh
    copies and its pages already read are not sent again (_split_batch)."""
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        earlier = connection.execute(
            text(
                """
                SELECT batch_id, original_filename, created_at_utc
                FROM auditcore.p2_upload_batches
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND sha256=:sha256
                  AND batch_id<>:batch_id AND batch_status NOT IN ('FAILED','PARTIAL_FAILURE','CANCELLED')
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

    batch_folder = root_of(str(batch["original_object_key"])) or (
        f"p2-documents/{work.tenant_id}/{work.journey_id}/{batch['batch_id']}"
    )
    page_records: list[dict[str, Any]] = []
    for page_number, (page_payload, page_content_type) in enumerate(pages, start=1):
        page_sha = _page_content_sha(page_payload, page_content_type)
        # Beside the original, in whichever layout the original was stored
        # (dated for new uploads; uc03_p2_object_keys).
        object_key = (
            f"{batch_folder}/pages/{page_number:04d}-{page_sha[:12]}.pdf"
            if page_content_type == "application/pdf"
            else f"{batch_folder}/pages/{page_number:04d}-{page_sha[:12]}"
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
        # One split at a time per journey (review of #469, 2026-10-21): two
        # re-uploads of the same failed file arriving together must not both
        # queue a fresh copy of the same page; the second sees the first's.
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"p2-split:{work.tenant_id}:{work.journey_id}"},
        )
        # The same scan already on this Journey (an earlier upload, or an
        # earlier page of this file) is recorded, shown, and not sent again --
        # unless that copy failed: a failed page, or a page merged into a
        # document that failed (review of #469), is replaced below.
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
                      AND NOT (q.queue_status='MERGED' AND EXISTS (
                          SELECT 1 FROM auditcore.p2_document_queue g
                          WHERE g.tenant_id=q.tenant_id AND g.queue_id=q.merged_into_queue_id
                            AND g.queue_status IN ('FAILED','DEAD_LETTER','CANCELLED')))
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
        fresh_shas = [r["pageSha"] for r in page_records if r["pageSha"] not in already]
        for record in page_records:
            if record["pageSha"] in already:
                continue
            # A failed page the day-end sweep already cleared (swept_pending_sql)
            # is replaced the same way: its resubmit task closes here.
            for row in connection.execute(
                text(
                    f"""
                    UPDATE auditcore.p2_document_queue AS q
                    SET queue_status='CANCELLED', last_error='SUPERSEDED_PAGE', status_reason=:reason,
                        updated_at_utc=now()
                    WHERE q.tenant_id=:tenant_id AND q.journey_id=:journey_id AND q.batch_id<>:batch_id
                      AND q.unit_kind='PAGE' AND q.page_sha256=:sha
                      AND (q.queue_status IN ('FAILED','DEAD_LETTER') OR {swept_pending_sql("q")})
                    RETURNING q.batch_id, q.queue_id, q.di_document_id
                    """
                ),
                {"tenant_id": work.tenant_id, "journey_id": work.journey_id, "batch_id": batch["batch_id"],
                 "sha": record["pageSha"],
                 "reason": (f"Uploaded again as page {record['pageNumber']} of {batch['original_filename']}; "
                            "this copy is replaced.")},
            ).mappings().all():
                superseded_batches.add(str(row["batch_id"]))
                withdraw_at_di(connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                               queue_id=row["queue_id"], di_document_id=row["di_document_id"],
                               why="SUPERSEDED_PAGE")
        # A failed document made from several pages (a GROUP unit) is replaced
        # once every one of its pages has come in again (review of #469).
        for row in connection.execute(
            text(
                f"""
                UPDATE auditcore.p2_document_queue AS g
                SET queue_status='CANCELLED', last_error='SUPERSEDED_PAGE', status_reason=:reason,
                    updated_at_utc=now()
                WHERE g.tenant_id=:tenant_id AND g.journey_id=:journey_id AND g.batch_id<>:batch_id
                  AND g.unit_kind='GROUP'
                  AND (g.queue_status IN ('FAILED','DEAD_LETTER') OR {swept_pending_sql("g")})
                  AND NOT EXISTS (
                      SELECT 1 FROM auditcore.p2_document_queue m
                      WHERE m.tenant_id=g.tenant_id AND m.merged_into_queue_id=g.queue_id
                        AND m.unit_kind='PAGE' AND NOT (m.page_sha256 = ANY(:shas)))
                  AND EXISTS (
                      SELECT 1 FROM auditcore.p2_document_queue m
                      WHERE m.tenant_id=g.tenant_id AND m.merged_into_queue_id=g.queue_id AND m.unit_kind='PAGE')
                RETURNING g.batch_id, g.queue_id, g.di_document_id
                """
            ),
            {"tenant_id": work.tenant_id, "journey_id": work.journey_id, "batch_id": batch["batch_id"],
             "shas": fresh_shas, "reason": f"Uploaded again in {batch['original_filename']}; this copy is replaced."},
        ).mappings().all():
            superseded_batches.add(str(row["batch_id"]))
            withdraw_at_di(connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                           queue_id=row["queue_id"], di_document_id=row["di_document_id"], why="SUPERSEDED_PAGE")
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


def _ensure_extra_requirement_rows(connection, *, tenant_id: str, journey_id: UUID) -> int:
    """A checklist row for every classified type on this journey that the
    tenant's catalogue does not list (a KYC form, a UPI screenshot, an
    optional type the profile left out). Without a row the type has no
    requirement ref, so its values could never be linked or stored, however
    well DI read it (decision 2026-10-01: every classified page gets a
    checklist home). The row is OPTIONAL and repeatable (its key starts with
    ``p2_extra_``, see uc03_pc_booking_documents._is_repeatable_requirement),
    so every copy of the type is held. Never shown as a checklist card: the
    screen builds its cards from the templates, not from these rows.
    Returns the number of rows created."""
    from audit_core.uc03_document_capture_v2 import _canonical_document_type

    registry = get_registry()
    classified = connection.execute(
        text(
            """
            SELECT DISTINCT template_key FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND template_key IS NOT NULL AND template_key <> 'supporting_document'
              AND queue_status NOT IN ('CANCELLED', 'MERGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalars().all()
    existing = {
        _canonical_document_type(str(row[0]))
        for row in connection.execute(
            text(
                """
                SELECT document_type_key FROM auditcore.journey_document_requirements
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND document_type_key IS NOT NULL
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )
    }
    created = 0
    for template_key in classified:
        template = registry.documents.get(str(template_key))
        if template is None or not template.di_types:
            continue
        document_type_key = template.di_types[0]
        if _canonical_document_type(document_type_key) in existing:
            continue
        process_area = template.stage if template.stage in ("BOOKING", "DELIVERY") else "BOOKING"
        inserted = connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, document_requirement_item_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, requirement_status, condition_snapshot
                ) VALUES (
                    CAST(:t AS varchar), CAST(:j AS uuid), NULL, CAST(:rk AS varchar),
                    CAST(:dt AS varchar), CAST(:pa AS varchar), 'OPTIONAL', 'PENDING', '{}'::jsonb
                )
                ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
                RETURNING journey_document_requirement_id
                """
            ),
            {"t": tenant_id, "j": journey_id, "rk": f"p2_extra_{template.key}",
             "dt": document_type_key, "pa": process_area},
        ).scalar_one_or_none()
        if inserted is not None:
            created += 1
            existing.add(_canonical_document_type(document_type_key))
            logger.info(
                "p2_extra_requirement_row_created",
                tenant_id=tenant_id,
                journey_id=str(journey_id),
                template_key=template.key,
                document_type_key=document_type_key,
                process_area=process_area,
            )
    return created


def _di_context_and_requirements(engine: Engine, work: WorkItem) -> tuple[str, str, list[str], dict[str, str]]:
    security_client = get_security_oauth_client()
    di_client = get_di_client()
    # Warm the service-token cache before any transaction opens, so the
    # context read below does not wait on Security inside a transaction.
    security_client.get_service_token(audience=_DI_AUDIENCE)
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        _ensure_p2_requirement_rows(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
        _ensure_extra_requirement_rows(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
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
        preparing = connection.execute(
            text(
                """
                UPDATE auditcore.p2_document_queue
                SET queue_status='DI_UPLOAD_PREPARING', updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                  AND queue_status NOT IN ('MERGED','FAILED','DEAD_LETTER','CANCELLED')
                RETURNING queue_id
                """
            ),
            {"tenant_id": work.tenant_id, "queue_id": queue_id},
        ).scalar_one_or_none()
    if preparing is None:
        return  # settled meanwhile (failed on the clock, removed): nothing to send

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
        failures = [f for f in (intent.get("failures") or []) if isinstance(f, dict)]
        codes = {str(f.get("errorCode") or "") for f in failures}
        # A refusal of the request itself (DI's CONFLICT: this page's intent
        # already holds a stored file under another mapping) answers the same
        # on every retry; only a technical failure is worth trying again
        # (2026-10-20: 83 CONFLICT retries on 21 pages in one day).
        business = bool(codes) and codes <= {"CONFLICT", "INVALID_REQUEST", "VALIDATION_ERROR"}
        if "CONFLICT" in codes and _adopt_stored_upload(
            engine, work, row, v2_client=v2_client, token=token, context_ref=context_ref, queue_id=queue_id,
            filename=filename, content_type=content_type,
        ):
            return
        raise DiCaptureV2Error(
            status_code=409 if business else 503,
            detail=json.dumps({
                "detail": "The document service refused this page's upload intent ("
                          + ", ".join(sorted(c for c in codes if c) or ["no reason given"]) + ").",
                "retryable": not business,
            }),
        )
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

    _record_submitted_page(engine, work, row, queue_id=queue_id, di_document_id=di_document_id,
                           filename=filename, content_type=content_type)


def _record_submitted_page(
    engine: Engine, work: WorkItem, row: Any, *, queue_id: UUID, di_document_id: UUID, filename: str,
    content_type: str,
) -> None:
    """Record that DI holds this page as ``di_document_id`` and start polling
    it -- unless the page was settled while it was being sent (failed by
    the 30-minute clock, removed): then DI's new document is withdrawn at
    once, so DI does not read a page Audit Core has given up (review of
    #469, 2026-10-21)."""
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        recorded = connection.execute(
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
                  AND queue_status NOT IN ('MERGED','FAILED','DEAD_LETTER','CANCELLED')
                RETURNING queue_id
                """
            ),
            {
                "tenant_id": work.tenant_id,
                "queue_id": queue_id,
                "document_id": di_document_id,
            },
        ).scalar_one_or_none()
        if recorded is None:
            withdraw_at_di(connection, tenant_id=work.tenant_id, journey_id=work.journey_id, queue_id=queue_id,
                           di_document_id=di_document_id, why="SENT_AFTER_THE_PAGE_WAS_SETTLED")
            logger.info("p2_sent_page_already_settled", di_document_id=str(di_document_id), **_work_fields(work))
            return
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
        _enqueue_journey_reconcile(connection, work=work)


def _adopt_stored_upload(
    engine: Engine, work: WorkItem, row: Any, *, v2_client: Any, token: str, context_ref: str, queue_id: UUID,
    filename: str, content_type: str,
) -> bool:
    """DI answered CONFLICT for a client upload id Audit Core itself issued:
    an earlier attempt's file was stored even though its finalize call timed
    out, and DI is reading it (2026-10-20 review, S7). Adopt that document
    instead of failing the page. False when DI holds no such document."""
    for phase in ("BOOKING", "DELIVERY"):
        listing = v2_client.list_documents(
            token=token, tenant_id=work.tenant_id, external_context_ref=context_ref, phase=phase,
        )
        for item in listing.get("documents") or []:
            if (str(item.get("clientUploadId")) == str(row["client_upload_id"]) and item.get("documentId")
                    and str(item.get("state") or "") not in ("RECEIVING", "DELETED")):
                logger.info("p2_conflict_upload_adopted", di_document_id=str(item["documentId"]),
                            di_state=item.get("state"), **_work_fields(work))
                _record_submitted_page(engine, work, row, queue_id=queue_id,
                                       di_document_id=UUID(str(item["documentId"])),
                                       filename=filename, content_type=content_type)
                return True
    return False


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
    # What to store as the page's last_error when it settles FAILED (the
    # failure code); None: DI's failureCode.
    last_error: str | None = None
    # How many attempts DI made (its attemptsMade), kept for the failure report.
    attempts: int | None = None


_QUALITY_FAILURE_PREFIX = "DI_QUALITY_"
_UNREADABLE_FAILURE_CODES = frozenset({"FILE_EMPTY", "INVALID_FILE_CONTENT", "CORRUPT", "UPLOAD_FAILED"})

# Product owner's retry rule (2026-10-21 12:24 IST, replacing the night
# attempt of 2026-10-20), and his decision of 2026-10-22: nothing is read
# again at night. Document Intelligence makes every retry itself
# (verigence-di retry_policy.py: T0, T0+10 and T0+20 minutes, then FAILED)
# and reports, per page, what failed (failureKind: AI / TECHNICAL / DOCUMENT
# / CONFIGURATION), whether the page passed its upload scan (scanPassed),
# whether it holds another attempt (nextAttemptAtUtc) and how many attempts
# it made (attemptsMade). Audit Core never stacks retries of its own on top;
# a FAILED page raises the PC's "Document to resubmit" task with the reason
# in plain words, and no file takes more than 30 minutes
# (fail_unsettled_pages). A page our own setup refused (Gemini refused our
# key or model) is FAILED like any other, says it is our setup problem, and
# the Team Lead is alerted; nobody reads it again by night (2026-10-22).
# DI codes for an AI failure that a later attempt can fix (DI's
# AI_RETRYABLE_FAILURE_CODES).
_AI_RETRYABLE_CODES = frozenset({
    "DOCUMENT_AI_RATE_LIMITED", "DOCUMENT_AI_UNAVAILABLE", "DOCUMENT_AI_RESPONSE_INVALID",
    "CLASSIFICATION_PROVIDER_ERROR", "EXTRACTION_PROVIDER_ERROR",
})
# The PC was told: "again after 10 minutes, and again after 20 minutes".
_AI_RETRYING_REASON = ("AI reading failed — it is tried again 10 and 20 minutes after the first try. "
                       "Nothing to do now.")
_TECHNICAL_RETRYING_REASON = ("Document service had a problem with this page — it is tried again 10 and 20 "
                              "minutes after the first try. Nothing to do yet.")
# A further attempt DI holds for a page whose reading had failed for good: a
# "Read again" -- one attempt (2026-10-20 re-review).
_READ_AGAIN_REASON = "Being read again (one attempt). Nothing to do now."
_AI_REFUSED_REASON = "AI couldn't read this page — please upload it again."
_NOT_SCANNED_REASON = "File couldn't be scanned — please reload it."
# Gemini refused our key or model (2026-10-21): never the PC's to fix. Since
# 2026-10-22 nothing re-reads it at night, so the page is uploaded again.
_SETUP_REASON = SETUP_PAGE_TEXT
_CONFIG_REASON = ("Document Intelligence is not set up to read this page on our side. Ask your Team Lead to "
                  "raise it with support, then retry the page from the card once it is fixed.")
NOT_READ_IN_TIME_CODE = "NOT_READ_IN_30_MIN"
NOT_READ_IN_TIME_REASON = "Not read within 30 minutes. Please upload this page again."


def _attempts_text(di_item: dict[str, Any]) -> str:
    """How many times the step was tried, as DI counted it (2026-10-21).
    One attempt -- or a count DI does not report -- says "tried once": DI
    refuses a retry that would start more than 25 minutes after it received
    the page, so a page that failed late (queued behind a burst) fails after
    one attempt, and "tried again automatically" would be untrue (review of
    #469, second review)."""
    attempts = di_item.get("attemptsMade")
    if isinstance(attempts, int) and attempts > 1:
        return f"tried {attempts} times (at once, then 10 and 20 minutes later)" if attempts == 3 \
            else f"tried {attempts} times"
    return "tried once"


def _di_failure_code(di_item: dict[str, Any]) -> str:
    """The code behind DI's verdict on a page: the reading failure's when
    reading failed, else the upload / classification failure's."""
    if str(di_item.get("processingStatus") or "") in ("FAILED", "RETRY_PENDING"):
        code = di_item.get("processingFailureCode") or di_item.get("failureCode")
    else:
        code = di_item.get("failureCode") or di_item.get("processingFailureCode")
    return str(code or "").upper()


def _failed_page_outcome(di_item: dict[str, Any]) -> PageOutcome:
    """A page DI has FAILED, told to the PC in plain words (product owner's
    rule 2026-10-21). A DI that does not report failureKind yet keeps the
    earlier wording."""
    kind = di_item.get("failureKind")
    code = _di_failure_code(di_item)
    attempts = di_item.get("attemptsMade") if isinstance(di_item.get("attemptsMade"), int) else None
    if kind == "CONFIGURATION":
        if code == SETUP_FAILURE_CODE:
            return PageOutcome("FAILED", _SETUP_REASON, code, attempts)
        return PageOutcome("FAILED", _CONFIG_REASON, code or None, attempts)
    if kind == "AI":
        if di_item.get("scanPassed") is not True:
            return PageOutcome("FAILED", _NOT_SCANNED_REASON, code or None, attempts)
        if code in _AI_RETRYABLE_CODES:
            return PageOutcome("FAILED", f"AI reading failed — {_attempts_text(di_item)}. Please upload this page "
                                         "again.", code, attempts)
        return PageOutcome("FAILED", _AI_REFUSED_REASON, code or None, attempts)
    if kind == "TECHNICAL":
        return PageOutcome("FAILED", f"Document service couldn't process this page — {_attempts_text(di_item)}. "
                                     "Retry the page from the card, or upload it again.", code or None, attempts)
    return PageOutcome("FAILED", _di_failure_reason(di_item), code or None, attempts)


def _in_hand_reason(di_item: dict[str, Any] | None) -> str | None:
    """Why a page DI still has in hand is waiting, when an attempt already
    failed and DI has scheduled the next."""
    kind = (di_item or {}).get("failureKind")
    if (di_item or {}).get("nextAttemptAtUtc") and str((di_item or {}).get("processingStatus") or "") == "FAILED":
        # DI's retries leave the document RETRY_PENDING; a document still
        # FAILED with an attempt queued is a Read again.
        return _READ_AGAIN_REASON
    if kind == "AI":
        return _AI_RETRYING_REASON
    if kind == "TECHNICAL":
        return _TECHNICAL_RETRYING_REASON
    return None


def _in_hand_state(di_item: dict[str, Any]) -> str:
    return "EXTRACTING" if str(di_item.get("state") or "") == "CLASSIFIED" else "CLASSIFYING"


def recovery_decision(
    *,
    di_item: dict[str, Any] | None,
    marker: str,
    submitted_at: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """What to do with a page the PC (marker "r") asked to recover, given
    what Document Intelligence holds for it: a page state to poll from
    ("SYNCING_TO_AUDIT_CORE" when DI has read it, so the worker copies;
    "EXTRACTING" or "CLASSIFYING" while DI still has it in hand), "REUPLOAD"
    when DI has given it up or no longer knows it, or "REREAD" when our setup
    refused a page DI had already identified (one more reading of the copy DI
    holds). Only a person's Retry gets here: there is no night look-up since
    2026-10-22, and nothing is ever read again by night. ``marker``,
    ``submitted_at`` and ``now`` are no longer used."""
    del marker, submitted_at, now
    state = str((di_item or {}).get("state") or "")
    processing = str((di_item or {}).get("processingStatus") or "")
    failed = state == "FAILED" or (state == "CLASSIFIED" and processing == "FAILED")
    if di_item is not None and failed and di_item.get("nextAttemptAtUtc"):
        # DI holds another attempt, queued or running (a Read again): poll
        # and copy its result, never tell the PC the page failed
        # (2026-10-20 review, B1).
        return _in_hand_state(di_item)
    if di_item is None or state in ("FAILED", "DELETED"):
        return "REUPLOAD"
    if state == "CLASSIFIED":
        if processing == "PROCESSED":
            return "SYNCING_TO_AUDIT_CORE"
        if processing == "FAILED":
            return "REREAD" if di_item is not None and di_item.get("failureKind") == "CONFIGURATION" else "REUPLOAD"
        return "EXTRACTING"
    return "CLASSIFYING"


def _recover_page(engine: Engine, work: WorkItem, row: dict[str, Any]) -> None:
    """A FAILED page the PC asked to recover (Retry): look at what Document
    Intelligence already holds for it before ever uploading it again
    (decision 2026-09-30: never re-read what DI already read)."""
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
    if decision == "REUPLOAD" and di_item is not None and str(di_item.get("state") or "") != "DELETED":
        # The page is sent again as a new DI document: the old one is deleted
        # first (as a retired fragment is), so DI cannot revive it and leave
        # the journey with two documents for one page (final check F1,
        # 2026-10-21). A failure here fails the look-up, which retries.
        v2_client.delete_document(
            token=token, tenant_id=work.tenant_id, external_context_ref=context_ref,
            document_id=str(row["di_document_id"]),
        )
        logger.info("p2_page_recovery_old_document_deleted", di_document_id=str(row["di_document_id"]),
                    **_work_fields(work))
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        if decision == "REREAD":
            request_page_reread(
                connection, tenant_id=work.tenant_id, journey_id=work.journey_id, queue_id=queue_id,
                requested_by=str(work.payload.get("uploadedBy") or "SYSTEM"), correlation_id=work.correlation_id,
            )
        elif decision == "REUPLOAD":
            requeue_page_for_ingest(
                connection, tenant_id=work.tenant_id, journey_id=work.journey_id, queue_id=queue_id,
                client_upload_id=str(row["client_upload_id"]), marker=marker,
                requested_by=str(work.payload.get("uploadedBy") or "SYSTEM"), correlation_id=work.correlation_id,
            )
        else:
            # DI has the page in hand or has read it: poll and copy.
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_document_queue
                    SET queue_status=:status, status_reason=:reason, last_error=NULL, updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                      AND queue_status NOT IN ('MERGED','FAILED','DEAD_LETTER','CANCELLED')
                    """
                ),
                {"tenant_id": work.tenant_id, "queue_id": queue_id, "status": decision,
                 "reason": _in_hand_reason(di_item) if decision != "SYNCING_TO_AUDIT_CORE" else None},
            )
            _enqueue_journey_reconcile(connection, work=work)


def _di_failure_reason(di_item: dict[str, Any] | None) -> str:
    """Why the document service failed a page, as the PC should read it: a
    scan-quality rejection is final and needs a re-scan, not a retry."""
    code = _di_failure_code(di_item or {})
    detail = str((di_item or {}).get("failureDetail") or "").strip().rstrip(".")
    if code.startswith(_QUALITY_FAILURE_PREFIX):
        return (f"Page rejected: {detail or 'it did not pass the scan quality check'}. "
                "Re-scan this page and upload it again; retrying will not help.")
    if code in _UNREADABLE_FAILURE_CODES:
        # The file failed DI's upload scan (2026-10-20 wording).
        return f"{_NOT_SCANNED_REASON[:-1]}{' (' + detail + ')' if detail else ''}."
    if code == "DOCUMENT_AI_CONTENT_BLOCKED":
        return "AI declined to read this page's content — check the page and upload it again."
    return "Document Intelligence could not read this page. Retry or re-upload it." + (f" ({detail})" if detail else "")


# Set on a page once the worker itself copied DI's result and there was
# nothing in it. Distinct from the wording the old timer-based path used, so
# the nightly sweep can tell a page that was genuinely read empty from one
# that was merely never copied.
_NOTHING_READ_REASON = "Document Intelligence read this page but found no values on it."
_NOT_QUEUED_REASON = (
    "Identified but not read: the booking already holds this document, or this document "
    "type is not read. Kept as supporting evidence."
)
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
        if di_item.get("nextAttemptAtUtc"):
            # DI has another attempt queued or running (2026-10-20 review, B1).
            return PageOutcome(_in_hand_state(di_item), _in_hand_reason(di_item))
        return _failed_page_outcome(di_item)
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
        if facts_copy == FACTS_COPY_GAVE_UP:
            return PageOutcome("FAILED", _FACTS_COPY_GAVE_UP_REASON)
        return PageOutcome("SYNCING_TO_AUDIT_CORE", None)
    if state == "CLASSIFIED":
        if di_item.get("extractionQueued") is False and processing in ("", "PENDING", "NOT_STARTED"):
            # DI classified the page and then left it alone (an extra copy
            # of a document the booking already holds, a type with no
            # checklist slot or no extraction profile). Nothing is ever
            # going to read it: settle it now rather than show "reading"
            # for ever (seen live 2026-10-01, a file stuck for 12 hours).
            return PageOutcome("SUPPORTING", _NOT_QUEUED_REASON)
        return PageOutcome("EXTRACTING", _in_hand_reason(di_item))
    return PageOutcome("CLASSIFYING", _in_hand_reason(di_item))


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
            facts_only=True,  # the Journey-wide work runs in STAGE_RECOMPUTE (2026-10-29)
        )
        return "COPIED"
    except DocumentSyncLockBusyError:
        return None
    except Exception as exc:
        transient = _is_transient_copy_failure(exc)
        logger.warning(
            "p2_page_facts_copy_failed",
            tenant_id=work.tenant_id,
            journey_id=str(work.journey_id),
            di_document_id=str(document_id),
            transient=transient,
            exc_info=True,
        )
        if transient:
            return None
        failures = _count_facts_copy_failure(engine, work=work, queue_id=page["queue_id"], exc=exc)
        if failures >= _FACTS_COPY_MAX_FAILURES:
            logger.error(
                "p2_page_facts_copy_gave_up",
                tenant_id=work.tenant_id,
                journey_id=str(work.journey_id),
                di_document_id=str(document_id),
                failures=failures,
                exc_type=type(exc).__name__,
            )
            return FACTS_COPY_GAVE_UP
        return None


# Decision 2026-10-19: a facts copy that fails for a reason that will not go
# away by waiting (a database rule refusing the write, a data error) used to
# be treated like "DI not finished yet": the page never settled, the
# reconcile rescheduled itself every five minutes for ever (a reschedule
# never counts toward the dead-letter limit) and held a worker slot for one
# to three minutes each time -- found live 2026-10-19, three Journeys, 47
# rounds in under three hours. After this many such failures in a row the
# page settles as FAILED with a plain reason instead; a PC's Retry resets
# the count (uc03_p2_runtime resets attempt_count).
_FACTS_COPY_MAX_FAILURES = 3
FACTS_COPY_GAVE_UP = "GAVE_UP"
_FACTS_COPY_GAVE_UP_REASON = (
    "Audit Core could not save this page's values (a system error, not a problem with the scan). "
    "Use Retry once it has been fixed."
)


def _is_transient_copy_failure(exc: BaseException) -> bool:
    """Failures that waiting can fix: a dependency (DI, Security) unreachable,
    the database busy or timing out. These keep the old "look again shortly"
    behaviour and never count toward giving up."""
    import httpx
    from sqlalchemy.exc import OperationalError

    from audit_core.di_client import DiClientError
    from audit_core.errors import DependencyUnavailableError
    from audit_core.security import SecurityTokenError as SecurityTokenErrorA
    from audit_core.security_integration import (
        SecurityTokenError as SecurityTokenErrorB,
    )

    return isinstance(
        exc,
        (
            OperationalError,
            DependencyUnavailableError,
            DiClientError,
            SecurityTokenErrorA,
            SecurityTokenErrorB,
            httpx.HTTPError,
            TimeoutError,
            ConnectionError,
        ),
    )


def _count_facts_copy_failure(engine: Engine, *, work: WorkItem, queue_id: Any, exc: BaseException) -> int:
    """One more consecutive non-transient facts-copy failure on this page;
    returns the count. Its own short transaction: the copy's own one has
    already rolled back."""
    try:
        with engine.begin() as connection:
            set_tenant_context(connection, work.tenant_id)
            return int(
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.p2_document_queue
                        SET attempt_count = attempt_count + 1,
                            last_error = :error,
                            updated_at_utc = now()
                        WHERE tenant_id=:t AND queue_id=:q
                        RETURNING attempt_count
                        """
                    ),
                    {"t": work.tenant_id, "q": queue_id,
                     "error": f"FACTS_COPY_FAILED: {type(exc).__name__}"},
                ).scalar_one_or_none()
                or 0
            )
    except Exception:
        logger.warning("p2_page_facts_copy_failure_not_counted", tenant_id=work.tenant_id, exc_info=True)
        return 0


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
            if outcome.status == "READY" or facts_copies.get(str(page["queue_id"])) == "COPIED":
                # A page whose facts were copied (READY or NEEDS_REVIEW) asks for
                # the one STAGE_RECOMPUTE that materialises them (facts_only copy).
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
                SELECT batch_id, grouping_status, original_filename, original_object_key
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
        folder = root_of(str(batch["original_object_key"])) or (
            f"p2-documents/{work.tenant_id}/{work.journey_id}/{batch_id}"
        )
        key = f"{folder}/groups/{label}-{digest[:12]}.pdf"
        storage.put_object(key, payload, content_type="application/pdf")
        merged_keys[document.page_numbers] = (key, digest)

    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        _owned(connection, work)
        # Grouping may have been failed on the clock while the pages were
        # being merged (fail_unsettled_pages): then nothing is written.
        still_grouping = connection.execute(
            text(
                """
                SELECT 1 FROM auditcore.p2_upload_batches
                WHERE tenant_id=:tenant_id AND batch_id=:batch_id AND grouping_status IN ('GROUPING','PENDING')
                FOR UPDATE
                """
            ),
            {"tenant_id": work.tenant_id, "batch_id": batch_id},
        ).scalar_one_or_none()
        if still_grouping is None:
            logger.info("p2_grouping_already_settled", **_work_fields(work))
            return
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
                      AND page_number = ANY(:page_numbers)
                      AND queue_status NOT IN ('MERGED','FAILED','DEAD_LETTER','CANCELLED')
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
                attempt_count=COALESCE(:attempts, attempt_count),
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
              -- grouping may merge the page while this reconcile runs: a
              -- merged page is final and never gets a stale DI state back;
              -- nor does a page the 30-minute clock failed meanwhile
              AND queue_status NOT IN ('MERGED','FAILED','DEAD_LETTER','CANCELLED')
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
            "failure_code": (
                (outcome.last_error or (di_item or {}).get("failureCode")) if outcome.status == "FAILED" else None
            ),
            # How many attempts DI made, for the failure report (2026-10-21).
            "attempts": outcome.attempts if outcome.status == "FAILED" else None,
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


def _page_reread(engine: Engine, work: WorkItem) -> None:
    """Ask Document Intelligence to read one page again (decision
    2026-10-01): the PC's "Read again" or the journey Re-sync (the nightly
    sweep no longer asks, 2026-10-22). DI never re-uploads or re-classifies; it queues one more reading
    of the document it holds, or says the page is already read, still in
    hand, or not classified. The page's own state follows that answer and
    the journey reconcile picks the result up."""
    queue_id = UUID(work.work_key)
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        row = connection.execute(
            text(
                """
                SELECT queue_status, di_document_id FROM auditcore.p2_document_queue
                WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                """
            ),
            {"tenant_id": work.tenant_id, "queue_id": queue_id},
        ).mappings().one_or_none()
    # A failed page is queued again by the request itself (request_page_reread);
    # one still FAILED here was failed again meanwhile (the 30-minute clock).
    if (row is None or row["queue_status"] in ("MERGED", "CANCELLED", "READY", "FAILED", "DEAD_LETTER")
            or row["di_document_id"] is None):
        logger.info("p2_page_reread_skipped", queue_id=str(queue_id),
                    queue_status=row["queue_status"] if row else None, **_work_fields(work))
        return
    context_ref, token, _, _ = _di_context_and_requirements(engine, work)
    outcome = get_di_capture_v2_client().reprocess_document(
        token=token, tenant_id=work.tenant_id, external_context_ref=context_ref,
        document_id=str(row["di_document_id"]),
    )
    result = str(outcome.get("outcome") or "")
    status = {
        "queued": "EXTRACTING",
        "in_progress": "EXTRACTING",
        "already_processed": "SYNCING_TO_AUDIT_CORE",
        "not_classified": "CLASSIFYING",
    }.get(result)
    logger.info(
        "p2_page_reread",
        queue_id=str(queue_id),
        di_document_id=str(row["di_document_id"]),
        di_outcome=result,
        processing_job_id=outcome.get("processingJobId"),
        new_status=status,
        **_work_fields(work),
    )
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        _owned(connection, work)
        if status is not None:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_document_queue
                    SET queue_status=:status, status_reason=NULL, last_error=NULL,
                        di_submitted_at_utc=now(), updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND queue_id=:queue_id
                      AND queue_status NOT IN ('MERGED', 'CANCELLED', 'READY', 'FAILED', 'DEAD_LETTER')
                    """
                ),
                {"tenant_id": work.tenant_id, "queue_id": queue_id, "status": status},
            )
        record_activity(
            connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
            event_type="PAGE_REREAD", subject_type="DOCUMENT_PAGE", subject_id=str(queue_id),
            details={"diOutcome": result, "processingJobId": outcome.get("processingJobId"),
                     "status": status},
            correlation_id=work.correlation_id,
        )
        _enqueue_journey_reconcile(connection, work=work, delay_seconds=5)


def withdraw_at_di(
    connection, *, tenant_id: str, journey_id: UUID, queue_id: Any, di_document_id: Any, why: str,
) -> None:
    """Queue the deletion of a page's Document Intelligence document: the
    page was failed on the 30-minute clock, uploaded again (superseded), or
    DI stored it after Audit Core had already given it up (review of #469,
    2026-10-21). DI stops working on it and nothing is read twice -- the
    same deletion _recover_page makes before it sends a page again (F1).
    A later Retry finds no DI document and sends the page afresh."""
    if di_document_id is None:
        return
    enqueue_work(
        connection, tenant_id=tenant_id, journey_id=journey_id, work_type=WITHDRAW_WORK_TYPE,
        work_key=str(di_document_id),
        payload={"queueId": str(queue_id), "diDocumentId": str(di_document_id), "why": why},
        correlation_id=None,
    )


def _withdraw_di_document(engine: Engine, work: WorkItem) -> None:
    """Delete one given-up page's document at DI (withdraw_at_di). DI answers
    the same whether or not it still holds the document."""
    context_ref, token, _, _ = _di_context_and_requirements(engine, work)
    get_di_capture_v2_client().delete_document(
        token=token, tenant_id=work.tenant_id, external_context_ref=context_ref,
        document_id=str(work.payload["diDocumentId"]),
    )
    logger.info("p2_di_document_withdrawn", di_document_id=str(work.payload["diDocumentId"]),
                queue_id=str(work.payload.get("queueId")), why=work.payload.get("why"), **_work_fields(work))


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
    # A page the day-end sweep cleared still counts as a failed page of the
    # file until it is resubmitted (swept_pending_sql), so the file does not
    # turn COMPLETED -- and its "Document to resubmit" task stays open.
    counts = connection.execute(
        text(
            f"""
            SELECT COUNT(*) FILTER (WHERE queue_status NOT IN ('CANCELLED','MERGED') OR swept) AS total,
                   COUNT(*) FILTER (WHERE queue_status IN ('READY','SUPPORTING','NEEDS_REVIEW')) AS usable,
                   COUNT(*) FILTER (WHERE queue_status IN ('FAILED','DEAD_LETTER') OR swept) AS failed
            FROM (
              SELECT q.queue_status, {swept_pending_sql("q")} AS swept
              FROM auditcore.p2_document_queue q
              WHERE q.tenant_id=:tenant_id AND q.batch_id=:batch_id
            ) pages
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
        "large_upload": sync_large_upload_tasks(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "unclassified_page": sync_unclassified_page_tasks(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "vehicle_photo": sync_vehicle_photo_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "delivery_review": sync_delivery_review_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
        "insurance_source": sync_insurance_source_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, evaluation_started_at=started_at),
    }
    for rule, counts in outcomes.items():
        # The single-task rules answer with what happened to their one task.
        as_counts = {counts: 1} if isinstance(counts, str) else (counts or {})
        changed = {k: v for k, v in as_counts.items() if k != "UNCHANGED" and v}
        if changed:
            logger.info("p2_rule_tasks", tenant_id=tenant_id, journey_id=str(journey_id), rule=rule, **changed)


def _materialize_journey(connection, *, tenant_id: str, journey_id: UUID) -> None:
    """Booking values, then the Booking and Delivery documents' canonical tables
    (insurance, registration, finance, invoices, receipts), each idempotent.
    A failure here is logged and does not stop the recompute; a deadlock fails
    the item so it is retried (uc03_db_transient)."""
    from audit_core.uc03_db_transient import raise_if_worker_transient
    from audit_core.uc03_delivery_post_extraction_materialization import (
        materialize_booking_documents_from_durable_store,
        materialize_delivery_documents_from_durable_store,
    )
    from audit_core.uc03_post_extraction_materialization import (
        materialize_machine_booking_values,
    )

    for step in (materialize_machine_booking_values, materialize_booking_documents_from_durable_store,
                 materialize_delivery_documents_from_durable_store):
        try:
            with connection.begin_nested():
                step(connection, tenant_id=tenant_id, journey_id=journey_id)
        except Exception as exc:
            raise_if_worker_transient(exc)
            logger.warning("p2_materialize_failed", step=step.__name__, tenant_id=tenant_id,
                           journey_id=str(journey_id), exc_info=True)


def _apply_file_identity(connection, *, tenant_id: str, journey_id: UUID) -> None:
    """A failure here is logged and does not stop the recompute (the booking
    is then rebuilt as it stands); a deadlock fails the item so it is retried."""
    from audit_core.uc03_db_transient import raise_if_worker_transient
    from audit_core.uc03_p2_file_identity import apply_file_identity

    try:
        with connection.begin_nested():
            apply_file_identity(connection, tenant_id=tenant_id, journey_id=journey_id)
    except Exception as exc:
        raise_if_worker_transient(exc)
        logger.warning("p2_file_identity_failed", tenant_id=tenant_id, journey_id=str(journey_id), exc_info=True)


def _bring_back_earlier_copies(connection, *, tenant_id: str, journey_id: UUID) -> None:
    """As _apply_file_identity: a failure is logged and the recompute goes on;
    a deadlock fails the item so it is retried."""
    from audit_core.uc03_db_transient import raise_if_worker_transient
    from audit_core.uc03_p2_earlier_copy import bring_back_earlier_copies

    try:
        with connection.begin_nested():
            bring_back_earlier_copies(connection, tenant_id=tenant_id, journey_id=journey_id)
    except Exception as exc:
        raise_if_worker_transient(exc)
        logger.warning("p2_earlier_copy_back_failed", tenant_id=tenant_id, journey_id=str(journey_id), exc_info=True)


def settle_journey(connection, *, tenant_id: str, journey_id: UUID,
                   started_at: datetime | None = None) -> dict[str, Any]:
    """Recompute the stage from the documents, raise / close the rule tasks
    that follow from it, and recompute once more (closing tasks can pass
    the last Delivery gate). Returns the settled result with every
    transition seen on the way."""
    # The Journey's canonical data from every document on file, once per
    # recompute (2026-10-29, rule storm): the per-page copy step copies the
    # page's facts only (facts_only), so a 20-page upload no longer rebuilds
    # the whole Journey 20 times, and a PC's correction reaches the Deal on the
    # recompute it requests. Identity, duplicate, SKU and payment checks run in
    # the NATIVE control units this recompute requests.
    # Everything below follows from the facts on file: the review tasks it
    # closes ask for no recompute of their own (2026-10-31, worker churn).
    mark_derived_writes(connection)
    # Whose each uploaded file is (2026-10-31): another customer's file dropped
    # into this booking leaves it before anything is rebuilt from the
    # documents, and the customer's own copies it had superseded come back
    # (uc03_p2_file_identity; acts only with P2_FILE_IDENTITY_ENFORCE on).
    _apply_file_identity(connection, tenant_id=tenant_id, journey_id=journey_id)
    # A copy whose replacement never became a document (its pages merged into
    # a document never read) is the booking's again (2026-11-01,
    # uc03_p2_earlier_copy), before the booking is rebuilt from its documents.
    _bring_back_earlier_copies(connection, tenant_id=tenant_id, journey_id=journey_id)
    _materialize_journey(connection, tenant_id=tenant_id, journey_id=journey_id)
    # The booking date follows the receipts still on file: a receipt deleted,
    # re-read or replaced since the last pass no longer sets it (2026-10-20).
    from audit_core.uc03_booking_confirmation_rules import (
        evaluate_minimum_booking_payment,
    )

    try:
        with connection.begin_nested():
            evaluate_minimum_booking_payment(connection, tenant_id=tenant_id, journey_id=journey_id,
                                             correlation_id="")
    except Exception:
        logger.warning("p2_booking_date_refresh_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                       exc_info=True)
    # The engine records STAGE_CHANGED only when the stage actually moves.
    named = sync_customer_name(connection, tenant_id=tenant_id, journey_id=journey_id)
    if named in ("VERIFIED", "CLEARED"):
        logger.info("p2_customer_named", tenant_id=tenant_id, journey_id=str(journey_id), how=named)
    # The entered name of a booking another customer's file was dropped into
    # follows the booking's own customer once that file is out (2026-11-01).
    entered = follow_entered_name(connection, tenant_id=tenant_id, journey_id=journey_id)
    if entered is not None:
        logger.info("p2_entered_name_follows_customer", tenant_id=tenant_id, journey_id=str(journey_id), name=entered)
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


# A page still in flight this long after its last change is reconciled by
# the nightly sweep (a status read, never a new attempt: 2026-10-20).
_NIGHTLY_SWEEP_AFTER_SECONDS = int(os.environ.get("P2_NIGHTLY_SWEEP_AFTER_SECONDS", str(24 * 60 * 60)))


def queue_nightly_upload_sweep(connection, *, tenant_id: str, now: datetime | None = None) -> dict[str, int]:
    """The night's copy safety net: reconcile any page still in flight after
    a day, and put a page DI has read but Audit Core holds no values for back
    in line for one copy of what DI already holds. Safe to call more than
    once.

    Product owner, 2026-10-22: nothing is read again at night. This sweep
    therefore no longer looks up pages our setup refused (DI re-read those
    once at night; it does not any more) and no longer asks DI to read a
    classified page again (PAGE_REREAD). A failed page is the PC's to
    resubmit; the day-end sweep (sweep_failed_pages) clears it from the
    Journey. What is left here only copies or polls what DI already read:
    it never asks DI or the AI to read a document again."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=_NIGHTLY_SWEEP_AFTER_SECONDS)
    journeys: set[UUID] = set()
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
    return {"reconciled": len(stuck), "journeys": len(journeys)}


def _stage_recompute(engine: Engine, work: WorkItem) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        _owned(connection, work, lock=False)
        version_before = connection.execute(
            text("SELECT fact_version FROM auditcore.p2_journey_runtime WHERE tenant_id=:t AND journey_id=:j"),
            {"t": work.tenant_id, "j": work.journey_id},
        ).scalar_one_or_none()
        result = settle_journey(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
        # The Journey list reads the Deal's own price variance, stored here.
        refresh_price_variance(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)
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
        # What this recompute derived (canonical tables, the SKU's evidence) is
        # not a new fact: store the fingerprint as it now stands, so the
        # 30-second fact sweep does not take the recompute's own writes for a
        # change made elsewhere and request it again (2026-10-29, rule storm).
        # Not when a fact change was announced meanwhile: that one still runs.
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_journey_runtime SET fact_fingerprint=:fp
                WHERE tenant_id=:t AND journey_id=:j AND fact_version IS NOT DISTINCT FROM :v
                """
            ),
            {"t": work.tenant_id, "j": work.journey_id, "v": version_before,
             "fp": fact_fingerprint(connection, tenant_id=work.tenant_id, journey_id=work.journey_id)},
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
        if source_type == "RESOLVER":
            # A resolver's task (cost sheet vs NDC, the SKU and deal checks)
            # is closed or returned by the resolver itself, which runs in the
            # recompute (the account statement, invoices) and in the NATIVE
            # units (SKU, deal): run both on fresh facts. Before 2026-10-31
            # there was no adapter: a PC's "Review document" on a cost-sheet
            # task was retried five times and gave up (dead letter).
            # Forced, as for a rule's task: an unchanged fingerprint must not
            # skip the very check the PC asked for.
            settle_journey(connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                           started_at=datetime.now(UTC))
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
    # Items that name a single control -- queued before the ledger existed, and
    # by auditcore.p2_requeue_verifying_controls on every fact change for each
    # task in VERIFYING: evaluate every unit, NOT forced (2026-10-29, rule
    # storm). A fact change already changes the units' fingerprints, so they
    # run; forcing re-ran every unit once per VERIFYING task on every change.
    # A PC's "done" still forces its re-check (TASK_VERIFY).
    with engine.begin() as connection:
        set_tenant_context(connection, work.tenant_id)
        request_control_evaluation(
            connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
            correlation_id=work.correlation_id, delay_seconds=0, force=False,
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


def _same_file_delete(engine: Engine, work: WorkItem) -> None:
    """Delete the same file uploaded again (product owner 2026-10-31). Runs as
    its own item, never inside the check that found it: Document Intelligence
    and R2 are called with no transaction open, and the Journey's own queue
    rows -- this one too -- go with it (JourneyDeleted)."""
    from audit_core.uc03_p2_same_file import (
        REMOVED,
        UploadInProgress,
        publish_alert,
        run_same_file_delete,
    )

    try:
        outcome = run_same_file_delete(
            engine, tenant_id=work.tenant_id, journey_id=work.journey_id,
            correlation_id=_work_correlation_id(work),
            security_client=get_security_oauth_client(), v2_client=get_di_capture_v2_client(),
            storage=get_p2_document_storage(),
            before_record=lambda connection: _owned(connection, work),
        )
    except UploadInProgress as exc:
        # Pages are still arriving: not deleted under the PC's upload.
        raise RescheduleWork(str(exc), delay_seconds=exc.delay_seconds) from None
    except LeaseLost:
        # The Journey may have gone another way (a Team Lead deleted it):
        # then its queue row went with it and the alert is still to go out.
        with engine.begin() as connection:
            set_tenant_context(connection, work.tenant_id)
            gone = connection.execute(
                text("SELECT NOT EXISTS (SELECT 1 FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j)"),
                {"t": work.tenant_id, "j": work.journey_id},
            ).scalar_one()
            if gone:
                publish_alert(connection, tenant_id=work.tenant_id, journey_id=work.journey_id, outcome=REMOVED)
        if gone:
            raise JourneyDeleted(f"Journey {work.journey_id} was already deleted") from None
        raise
    if outcome in ("DELETED", "GONE"):
        raise JourneyDeleted(f"Journey {work.journey_id} deleted as the same file uploaded again")


def process_work(engine: Engine, work: WorkItem) -> None:
    handlers = {
        "SPLIT_BATCH": _split_batch,
        "DOCUMENT_INGEST": _ingest_document,
        "DOCUMENT_RECONCILE": _legacy_document_reconcile,
        "JOURNEY_RECONCILE": _journey_reconcile,
        "PAGE_REREAD": _page_reread,
        "BATCH_GROUP": _group_batch,
        WITHDRAW_WORK_TYPE: _withdraw_di_document,
        "STAGE_RECOMPUTE": _stage_recompute,
        "TASK_VERIFY": _task_verify,
        "CONTROL_EVALUATE": _control_evaluate,
        SAME_FILE_DELETE_WORK_TYPE: _same_file_delete,
    }
    handler = handlers.get(work.work_type)
    if handler is None:
        raise ValueError(f"Unsupported P2 work type {work.work_type}")
    if work.work_type in _PAGE_RETRY_WORK_TYPES and work.attempt_count > 0:
        # An automatic retry already scheduled when the PC closed the page's
        # task does not run (product owner 2026-10-27; uc03_p2_retry_stop).
        with engine.begin() as connection:
            set_tenant_context(connection, work.tenant_id)
            _owned(connection, work)
            why = automatic_retry_stop_reason(connection, tenant_id=work.tenant_id, journey_id=work.journey_id,
                                              queue_id=work.work_key)
            if why:
                _stop_automatic_retry(connection, work, why=why, error=None, attempts=None, cancel_work=False)
        if why:
            return
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
# A cache only: the durable claim is p2_scheduled_runs (claim_scheduled_run).
_last_nightly_review: dict[str, str] = {}
# Same for the day-end sweep (claim_scheduled_run, run name P2_DAYEND_SWEEP).
_last_dayend_sweep: dict[str, str] = {}
_rollout_done: set[str] = set()
_last_watchdog_at: dict[str, float] = {}
_WATCHDOG_SECONDS = 60
# Task Manager pass (recurring occurrences, gate pass tasks): every 5 minutes.
_last_task_manager_at: dict[str, float] = {}
_TASK_MANAGER_SECONDS = 300
_RETRY_POLICY_RUN = "RETRY_POLICY_2026_10_21"
_ROLLOUT_REASON = ("Retrying now (the automatic retries were shortened to three attempts within 20 minutes). "
                   "Nothing to do yet.")


def apply_retry_policy_rollout(connection, *, tenant_id: str) -> int | None:
    """One-time deploy step of the retry rule (2026-10-20 review, S5), claimed
    durably so it runs once per tenant: a page or upload waiting on an older,
    longer ladder is retried now, under the rule of 2026-10-21 (three
    attempts within 20 minutes), with a reason that says so. Returns how many
    waits were pulled forward, or None when the step already ran."""
    if not claim_scheduled_run(connection, tenant_id=tenant_id, run_name=_RETRY_POLICY_RUN, run_key="rollout",
                               claimed_by=_WORKER_ID):
        return None
    pulled = connection.execute(
        text(
            """
            UPDATE auditcore.p2_work_queue
            SET next_attempt_at_utc=now(), updated_at_utc=now()
            WHERE tenant_id=:t AND work_type IN ('DOCUMENT_INGEST','SPLIT_BATCH')
              AND work_status='RETRY_WAIT'
            RETURNING work_type, work_key
            """
        ),
        {"t": tenant_id},
    ).mappings().all()
    pages = [UUID(r["work_key"]) for r in pulled if r["work_type"] == "DOCUMENT_INGEST"]
    if pages:
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_document_queue
                SET status_reason=:reason, updated_at_utc=now()
                WHERE tenant_id=:t AND queue_id = ANY(:pages) AND queue_status='RETRY_WAIT'
                """
            ),
            {"t": tenant_id, "pages": pages, "reason": _ROLLOUT_REASON},
        )
    return len(pulled)


# Product owner, 2026-10-21: "No file should take more than 30 mins". A page
# (or a file not yet split into pages) still not settled 30 minutes after it
# was uploaded -- or after a person's Retry / Read again restarted its clock
# -- is failed, polling included, and the PC is asked to resubmit it. A page
# Document Intelligence has read (SYNCING_TO_AUDIT_CORE: Audit Core is
# copying its values) counts as read.
READ_CEILING = timedelta(minutes=30)
# Grouping (review of #469, 2026-10-21): the pages of one document are put
# together only once every page of the file is identified. That gets no extra
# time: a document made by grouping is bound by the same 30 minutes from the
# upload ("No file should take more than 30 mins"), so a file never waits
# longer than that before it is read or failed and resubmitted. (An earlier
# draft gave grouping 40 minutes; the independent review found that broke the
# rule.) GROUPING_CEILING stays a named constant so the rule is read in one
# place, and is tied to READ_CEILING so the two cannot drift apart.
GROUPING_CEILING = READ_CEILING
_UNSETTLED_PAGE_STATES = tuple(s for s in _PAGE_ACTIVE_STATES if s != "SYNCING_TO_AUDIT_CORE")
_UNSETTLED_BATCH_STATES = ("AWAITING_UPLOAD", "UPLOADED", "SPLITTING")
GROUPING_FAILED_CODE = "GROUPING_FAILED"
GROUPING_FAILED_REASON = ("The pages of this document could not be put together and read. "
                          "Please upload the file again.")


def _fail_grouping(connection, *, tenant_id: str, batch_id: UUID, why: str) -> list[dict[str, Any]]:
    """Grouping of a file stopped (it ran out of time, or its work stopped):
    the pages that were waiting to be put together into one document -- a
    type only ever read whole, identified but not read -- are FAILED, so the
    file's "Document to resubmit" task names them (review of #469). Returns
    the pages failed."""
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_upload_batches
            SET grouping_status='FAILED', updated_at_utc=now()
            WHERE tenant_id=:t AND batch_id=:b AND grouping_status IN ('PENDING','GROUPING')
            """
        ),
        {"t": tenant_id, "b": batch_id},
    )
    pages = connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status='FAILED', status_reason=:reason, last_error=:code, updated_at_utc=now()
            WHERE tenant_id=:t AND batch_id=:b AND unit_kind='PAGE' AND queue_status='SUPPORTING'
              AND type_overridden_by_actor_id IS NULL AND classified_document_type = ANY(:types)
            RETURNING queue_id, journey_id, batch_id, di_document_id, last_error
            """
        ),
        {"t": tenant_id, "b": batch_id, "reason": GROUPING_FAILED_REASON, "code": GROUPING_FAILED_CODE,
         "types": sorted(read_once_di_types(get_registry()))},
    ).mappings().all()
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_work_queue
            SET work_status='CANCELLED', next_attempt_at_utc=NULL, last_error=:why, updated_at_utc=now()
            WHERE tenant_id=:t AND work_type='BATCH_GROUP' AND work_key=:k AND work_status IN ('PENDING','RETRY_WAIT')
            """
        ),
        {"t": tenant_id, "k": str(batch_id), "why": why},
    )
    _refresh_batch_status(connection, tenant_id, batch_id)
    return [dict(r) for r in pages]


def fail_unsettled_pages(connection, *, tenant_id: str, now: datetime | None = None) -> int:
    """The 30-minute ceiling (product owner 2026-10-21; review of #469).

    - Every page still not settled 30 minutes after its clock started is
      FAILED with "Not read within 30 minutes".
      A document made by grouping several pages gets the same 30 minutes
      from the upload (GROUPING_CEILING is READ_CEILING).
    - Every file not uploaded or split into pages within 30 minutes is
      FAILED; grouping not done within 30 minutes fails the pages that
      waited for it (_fail_grouping).
    - Their pending send / read-again / split / grouping work is cancelled,
      and the DI document of every page failed here is withdrawn (deleted at
      DI: it stops reading it, nothing is read twice).
    - Work already running never writes over these FAILED pages (every page
      write from running work is guarded); a page it sends to DI meanwhile
      is withdrawn at once (_record_submitted_page).
    - The file's resubmit task is raised for the PC.
    Returns the number of pages and files failed."""
    now = now or datetime.now(UTC)
    cutoff = now - READ_CEILING
    group_cutoff = now - GROUPING_CEILING
    pages = [dict(r) for r in connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue AS q
            SET queue_status='FAILED', status_reason=:reason, last_error=:code,
                next_attempt_at_utc=NULL,
                updated_at_utc=now()
            FROM auditcore.p2_upload_batches AS b
            WHERE q.tenant_id=:t AND b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
              AND q.queue_status = ANY(:states)
              AND (
                (q.read_clock_started_at_utc IS NOT NULL AND q.read_clock_started_at_utc < :cutoff)
                OR (q.read_clock_started_at_utc IS NULL AND q.unit_kind <> 'GROUP' AND b.created_at_utc < :cutoff)
                OR (q.read_clock_started_at_utc IS NULL AND q.unit_kind = 'GROUP' AND b.created_at_utc < :group_cutoff)
              )
            RETURNING q.queue_id, q.journey_id, q.batch_id, q.di_document_id, q.last_error
            """
        ),
        {"t": tenant_id, "states": list(_UNSETTLED_PAGE_STATES), "cutoff": cutoff, "group_cutoff": group_cutoff,
         "reason": NOT_READ_IN_TIME_REASON, "code": NOT_READ_IN_TIME_CODE},
    ).mappings().all()]
    files = connection.execute(
        text(
            """
            UPDATE auditcore.p2_upload_batches
            SET batch_status='FAILED',
                failure_reason=CASE WHEN batch_status='AWAITING_UPLOAD' THEN :not_uploaded ELSE :not_split END,
                updated_at_utc=now()
            WHERE tenant_id=:t AND batch_status = ANY(:states) AND page_count=0 AND created_at_utc < :cutoff
            RETURNING batch_id, journey_id
            """
        ),
        {"t": tenant_id, "states": list(_UNSETTLED_BATCH_STATES), "cutoff": cutoff,
         "not_uploaded": FILE_NOT_UPLOADED_IN_TIME_REASON, "not_split": FILE_NOT_SPLIT_IN_TIME_REASON},
    ).mappings().all()
    late_groupings = connection.execute(
        text(
            """
            SELECT batch_id, journey_id FROM auditcore.p2_upload_batches
            WHERE tenant_id=:t AND grouping_status IN ('PENDING','GROUPING') AND created_at_utc < :cutoff
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"t": tenant_id, "cutoff": group_cutoff},
    ).mappings().all()
    grouping_pages: list[dict[str, Any]] = []
    for batch in late_groupings:
        grouping_pages += _fail_grouping(connection, tenant_id=tenant_id, batch_id=UUID(str(batch["batch_id"])),
                                         why=NOT_READ_IN_TIME_CODE)
    if not pages and not files and not late_groupings:
        return 0
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_work_queue
            SET work_status='CANCELLED', next_attempt_at_utc=NULL, last_error=:why, updated_at_utc=now()
            WHERE tenant_id=:t AND work_status IN ('PENDING','RETRY_WAIT')
              AND ((work_type IN ('DOCUMENT_INGEST','PAGE_REREAD') AND work_key = ANY(:pages))
                   OR (work_type IN ('SPLIT_BATCH','BATCH_GROUP') AND work_key = ANY(:files)))
            """
        ),
        {"t": tenant_id, "why": NOT_READ_IN_TIME_CODE,
         "pages": [str(r["queue_id"]) for r in pages], "files": [str(r["batch_id"]) for r in files]},
    )
    for batch_id in dict.fromkeys(UUID(str(r["batch_id"])) for r in pages):
        _refresh_batch_status(connection, tenant_id, batch_id)
    for row in [*pages, *grouping_pages]:
        withdraw_at_di(connection, tenant_id=tenant_id, journey_id=UUID(str(row["journey_id"])),
                       queue_id=row["queue_id"], di_document_id=row["di_document_id"],
                       why=NOT_READ_IN_TIME_CODE)
    journeys = dict.fromkeys(UUID(str(row["journey_id"])) for row in [*pages, *files, *late_groupings])
    for row in [*pages, *grouping_pages]:
        record_activity(
            connection, tenant_id=tenant_id, journey_id=UUID(str(row["journey_id"])),
            event_type="PAGE_NOT_READ_IN_TIME", subject_type="DOCUMENT_PAGE", subject_id=str(row["queue_id"]),
            details={"ceilingMinutes": 30}, correlation_id=None,
        )
    for journey_id in journeys:
        sync_processing_failure_tasks(connection, tenant_id=tenant_id, journey_id=journey_id)
    return len(pages) + len(files) + len(grouping_pages)


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


def _dayend_sweep_due(tenant_id: str, now: datetime | None = None) -> str | None:
    """The sweep slot this worker still owes: the latest configured time
    (UTC) that has passed, at most _DAYEND_CATCHUP ago, named by its UTC date
    (20:30 UTC on the 22nd is 02:00 IST on the 23rd, and is claimed as
    "2026-10-22"). None when no slot is open or this worker already ran it.
    A worker that was down at the time still runs the missed slot when it is
    back within the catch-up; later that night is skipped and the next
    night's sweep takes what failed."""
    now = now or datetime.now(UTC)
    hour, _, minute = _runtime.DAYEND_SWEEP_UTC.partition(":")
    slot = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if now < slot:
        slot -= timedelta(days=1)
    if now - slot >= _DAYEND_CATCHUP:
        return None
    key = slot.date().isoformat()
    return None if _last_dayend_sweep.get(tenant_id) == key else key


def run_dayend_sweep(engine: Engine, tenant_id: str, now: datetime | None = None) -> dict[str, int] | None:
    """Run the night's sweep of failed pages once (product owner 2026-10-22).

    The slot is claimed durably (p2_scheduled_runs, run P2_DAYEND_SWEEP, the
    UTC date of the slot) in the same transaction as the sweep, so two worker instances,
    or a restart, never run it twice: the second claim finds the row and does
    nothing, and a run that fails rolls its claim back so the next pass tries
    again. Returns what the sweep did, or None when it is not due, was
    claimed by another instance, or failed."""
    from audit_core.uc03_p2_page_removal import SWEEP_RUN_NAME, sweep_failed_pages

    due = _dayend_sweep_due(tenant_id, now)
    if due is None:
        return None
    try:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            if not claim_scheduled_run(connection, tenant_id=tenant_id, run_name=SWEEP_RUN_NAME, run_key=due,
                                       claimed_by=_WORKER_ID):
                _last_dayend_sweep[tenant_id] = due
                return None
            result = sweep_failed_pages(connection, tenant_id=tenant_id, now=now)
        _last_dayend_sweep[tenant_id] = due
        logger.info("p2_dayend_sweep", tenant_id=tenant_id, day=due, **result)
        return result
    except Exception:
        logger.warning("p2_dayend_sweep_failed", tenant_id=tenant_id, day=due, exc_info=True)
        return None


def run_task_manager(engine: Engine, tenant_id: str, now: datetime | None = None) -> dict[str, int] | None:
    """The Task Manager's pass for one Project (docs/TASK_MANAGER_DESIGN_v1.0.md):
    raise today's recurring occurrences (each claimed once in
    p2_scheduled_runs), close finished gate pass tasks, mark unfinished ones
    incomplete. A failure is logged and retried on the next pass."""
    from audit_core.task_manager import run_tick

    try:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            result = run_tick(connection, tenant_id=tenant_id, now=now, claimed_by=_WORKER_ID)
        if any(result.values()):
            logger.info("task_manager_tick", tenant_id=tenant_id, **result)
        return result
    except Exception:
        logger.warning("task_manager_tick_failed", tenant_id=tenant_id, exc_info=True)
        return None


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
    try:
        filled = backfill_price_variance(engine, tenant_id=tenant_id)
        if filled:
            logger.info("p2_price_variance_backfilled", tenant_id=tenant_id, journeys=filled)
    except Exception:
        logger.warning("p2_price_variance_backfill_failed", tenant_id=tenant_id, exc_info=True)
    if tenant_id not in _rollout_done:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                pulled = apply_retry_policy_rollout(connection, tenant_id=tenant_id)
            _rollout_done.add(tenant_id)
            if pulled is not None:
                logger.info("p2_retry_policy_rollout", tenant_id=tenant_id, retries_pulled_forward=pulled)
        except Exception:
            logger.warning("p2_retry_policy_rollout_failed", tenant_id=tenant_id, exc_info=True)
    if now - _last_watchdog_at.get(tenant_id, 0.0) >= _WATCHDOG_SECONDS:
        _last_watchdog_at[tenant_id] = now
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                failed = fail_unsettled_pages(connection, tenant_id=tenant_id)
            if failed:
                logger.info("p2_not_read_in_time", tenant_id=tenant_id, pages=failed)
        except Exception:
            logger.warning("p2_watchdog_failed", tenant_id=tenant_id, exc_info=True)
    run_dayend_sweep(engine, tenant_id)
    if now - _last_task_manager_at.get(tenant_id, 0.0) >= _TASK_MANAGER_SECONDS:
        _last_task_manager_at[tenant_id] = now
        run_task_manager(engine, tenant_id)
    due = _nightly_review_due(tenant_id)
    if due is None:
        return
    # Re-run the Delivery checks for every delivery in progress: the
    # time-based checks fire without a document event. Idempotent per night.
    # The nightly SKU retry is switched off (product owner, 2026-10-22): SKU
    # is run on demand from Administration > Run SKU Resolution.
    from audit_core.uc03_p2_audit_rules import (
        queue_nightly_review,
    )

    try:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            # Claimed durably with the work it guards (2026-10-20 review): a
            # worker restart later the same night never runs the night again.
            if not claim_scheduled_run(connection, tenant_id=tenant_id, run_name="P2_NIGHTLY_REVIEW",
                                       run_key=due, claimed_by=_WORKER_ID):
                _last_nightly_review[tenant_id] = due
                return
            queued = queue_nightly_review(connection, tenant_id=tenant_id)
            swept = queue_nightly_upload_sweep(connection, tenant_id=tenant_id)
        _last_nightly_review[tenant_id] = due
        logger.info("p2_nightly_review_queued", tenant_id=tenant_id, night=due, deliveries_reviewed=queued, **swept)
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


def _settle_finished(engine: Engine, work: WorkItem, future: Future) -> None:
    started = _started_at.pop(work.work_id, None)
    duration_ms = round((time.perf_counter() - started) * 1000.0, 1) if started else None
    try:
        future.result()
    except RescheduleWork as exc:
        logger.info("p2_work_rescheduled", retry_in_seconds=exc.delay_seconds,
                    reason=redact_text(str(exc), 200), duration_ms=duration_ms, **_work_fields(work))
        _settle(engine, work, _reschedule, exc)
    except JourneyDeleted:
        logger.info("p2_work_completed", duration_ms=duration_ms, journey_deleted=True, **_work_fields(work))
    except LeaseLost:
        logger.warning("p2_work_lease_lost", duration_ms=duration_ms, **_work_fields(work))
    except Exception as exc:  # noqa: BLE001 - every failure is settled (retry/dead-letter)
        # _fail logs p2_work_retry_scheduled / p2_work_dead_lettered with the summary.
        _settle(engine, work, _fail, exc)
    else:
        logger.info("p2_work_completed", duration_ms=duration_ms, **_work_fields(work))
        _settle(engine, work, _complete)


def _run_claimed(engine: Engine, claimed: list[WorkItem]) -> None:
    with ThreadPoolExecutor(max_workers=_WORKER_CONCURRENCY) as pool:
        futures = {pool.submit(_process_in_context, engine, item): item for item in claimed}
        for future in as_completed(futures):
            _settle_finished(engine, futures[future], future)


# ---------------------------------------------------------------- continuous loop
# 2026-10-27 review of 26 Oct's "not read within 30 minutes" pages: the worker
# was restarted about every 5-10 minutes in the afternoon (each push to dev),
# and every restart left its claimed items locked for the 10-minute lease
# before anyone could pick them up; and run_once waited for the slowest item
# of a batch before claiming more. The worker now claims as soon as a slot is
# free, and on SIGTERM stops claiming, lets running items finish for a few
# seconds and releases the rest at once so the next worker takes them.
_stopping = threading.Event()
_inflight: dict[UUID, WorkItem] = {}
_inflight_lock = threading.Lock()
_DRAIN_SECONDS = float(os.environ.get("P2_WORKER_DRAIN_SECONDS", "15"))


def request_stop(*_: Any) -> None:
    """SIGTERM / SIGINT: finish what is running, take nothing new."""
    if not _stopping.is_set():
        logger.info("p2_worker_stopping", worker_id=_WORKER_ID, inflight=len(_inflight))
    _stopping.set()


def _renew_inflight_leases(engine: Engine) -> None:
    with _inflight_lock:
        running = list(_inflight.values())
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


def release_inflight_leases(engine: Engine, items: list[WorkItem] | None = None) -> int:
    """Let another worker take these items now instead of after the lease runs
    out. Safe while they still run here: whatever finishes first writes, the
    other one's write is refused by the lease token (LeaseLost)."""
    if items is None:
        with _inflight_lock:
            items = list(_inflight.values())
    released = 0
    for tenant_id in {item.tenant_id for item in items}:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                for item in (i for i in items if i.tenant_id == tenant_id):
                    released += connection.execute(
                        text(
                            """
                            UPDATE auditcore.p2_work_queue
                            SET lease_expires_at_utc=now()
                            WHERE tenant_id=:tenant_id AND work_id=:work_id AND lease_token=:token
                              AND work_status IN ('CLAIMED','PROCESSING')
                            """
                        ),
                        {"tenant_id": tenant_id, "work_id": item.work_id, "token": item.lease_token},
                    ).rowcount or 0
        except Exception as exc:  # noqa: BLE001 - the lease expiry stays the fallback
            logger.warning("p2_work_lease_release_failed", tenant_id=tenant_id, **exception_summary(exc))
    return released


def _claim_free_slots(engine: Engine, free: int) -> list[WorkItem]:
    claimed: list[WorkItem] = []
    for tenant_id in _active_tenants(engine):
        _maybe_sweep(engine, tenant_id)
        remaining = free - len(claimed)
        if remaining > 0 and not _stopping.is_set():
            claimed.extend(_claim_for_tenant(engine, tenant_id, remaining))
    return claimed


def run_forever(engine: Engine, *, stop: threading.Event | None = None, max_idle_cycles: int | None = None) -> None:
    """Keep up to _WORKER_CONCURRENCY items running; claim the moment a slot
    frees. ``max_idle_cycles`` ends the loop after that many empty polls (tests)."""
    stop = stop or _stopping
    pool = ThreadPoolExecutor(max_workers=_WORKER_CONCURRENCY, thread_name_prefix="p2-work")
    futures: dict[Future, WorkItem] = {}
    renewer_stop = threading.Event()

    def renew() -> None:
        while not renewer_stop.wait(_LEASE_RENEW_SECONDS):
            _renew_inflight_leases(engine)

    threading.Thread(target=renew, name="lease-renewer", daemon=True).start()
    last_heartbeat = time.monotonic()
    cycles = failed_cycles = idle = 0
    try:
        while not stop.is_set():
            cycles += 1
            free = _WORKER_CONCURRENCY - len(futures)
            claimed: list[WorkItem] = []
            if free > 0:
                try:
                    claimed = _claim_free_slots(engine, free)
                except Exception as exc:  # noqa: BLE001 - a database blip must not kill the worker
                    failed_cycles += 1
                    logger.error("p2_worker_cycle_failed", error_category="TECHNICAL", **exception_summary(exc))
            for item in claimed:
                with _inflight_lock:
                    _inflight[item.work_id] = item
                futures[pool.submit(_process_in_context, engine, item)] = item
            if futures:
                idle = 0
                done, _ = wait(list(futures), timeout=_POLL_SECONDS, return_when=FIRST_COMPLETED)
                for future in done:
                    item = futures.pop(future)
                    with _inflight_lock:
                        _inflight.pop(item.work_id, None)
                    _settle_finished(engine, item, future)
                    _stats["processed"] += 1
            else:
                idle += 1
                if max_idle_cycles is not None and idle >= max_idle_cycles:
                    break
                stop.wait(_POLL_SECONDS)
            if time.monotonic() - last_heartbeat >= _HEARTBEAT_SECONDS:
                logger.info("p2_worker_heartbeat", worker_id=_WORKER_ID, cycles=cycles,
                            failed_cycles=failed_cycles, processed=_stats["processed"], inflight=len(futures),
                            **_queue_depth(engine))
                last_heartbeat = time.monotonic()
                cycles = failed_cycles = 0
                _stats["processed"] = 0
    finally:
        if futures:
            # Stopping: give running items a moment, then hand the rest over.
            done, _ = wait(list(futures), timeout=_DRAIN_SECONDS)
            for future in done:
                item = futures.pop(future)
                with _inflight_lock:
                    _inflight.pop(item.work_id, None)
                _settle_finished(engine, item, future)
            released = release_inflight_leases(engine, list(futures.values()))
            logger.info("p2_worker_stopped", worker_id=_WORKER_ID, finished=len(done), released=released)
        renewer_stop.set()
        pool.shutdown(wait=False, cancel_futures=True)


_started_at: dict[UUID, float] = {}
_stats = {"processed": 0}


def _process_in_context(engine: Engine, work: WorkItem) -> None:
    """Run one item with its identifiers bound to every log line it produces (pool threads do
    not inherit context) and its correlation id sent on every outbound call."""
    _started_at[work.work_id] = time.perf_counter()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**_work_fields(work))
    try:
        # A deadlock inside a step fails the item, which is retried (uc03_db_transient).
        with work_item_scope():
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
                          EXTRACT(EPOCH FROM now() - min(queued_at_utc)
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


def _sku_run_once(engine: Engine, tag: str) -> None:
    """A full SKU resolution run over every Journey of every active Project,
    started from outside by setting AUDIT_CORE_SKU_RUN_TAG on the worker (no
    browser session to expire part-way). Runs once per tag: a restart with
    the same tag does not run it again. Each Journey's outcome is logged
    (``sku_run_item``) and kept against the run.

    A tag starting "unresolved" leaves every pinned SKU as it is. A tag
    starting "reresolve" re-checks every SKU under the words-first rules
    (``reresolve_all_skus``, review 471 B2): a DRY RUN that writes only the
    run items and one ``sku_reresolve_item`` line per Journey (old SKU ->
    proposed SKU, basis, words, prices, the task that would be raised),
    unless the tag contains "-commit" ("reresolve-2026-10-22-commit"), which
    writes the decisions. Nothing is cleared first; a SKU a PC or Team Lead
    set is never touched."""
    from audit_core.uc03_backfill_document_sync_producers import (
        CommitNotAllowed,
        backfill_sku_resolution_for_tenant,
        check_numbers_text,
        reresolve_all_skus,
    )

    actor = f"worker:{tag}"[:120]
    for tenant_id in _active_tenants(engine):
        try:
            # A run row exists from the moment the run starts, so "a row
            # exists" used to mean "already done" -- and a run the platform
            # killed half way through (a deploy replaces the container: it
            # happened twice on 2026-10-24) was abandoned for good, while the
            # log said "already done". It is done only when it says COMPLETE;
            # anything else resumes from the Journey it reached, which is
            # exact because the run pages in a stable order
            # (created_at_utc, journey_id).
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                previous = connection.execute(
                    text(
                        """
                        SELECT r.run_id, r.status,
                               (SELECT count(*) FROM auditcore.sku_resolution_run_items i
                                 WHERE i.tenant_id = r.tenant_id AND i.run_id = r.run_id) AS done_items
                        FROM auditcore.sku_resolution_runs r
                        WHERE r.tenant_id = :t AND r.started_by_actor_id = :a
                        ORDER BY r.started_at_utc DESC
                        LIMIT 1
                        """
                    ),
                    {"t": tenant_id, "a": actor},
                ).mappings().one_or_none()
            if previous is not None and previous["status"] == "COMPLETE":
                logger.info("sku_run_already_done", tenant_id=tenant_id, tag=tag)
                continue
            resume_from = int(previous["done_items"]) if previous is not None else 0
            if previous is not None:
                logger.info("sku_run_resuming", tenant_id=tenant_id, tag=tag,
                            run_id=str(previous["run_id"]), status=previous["status"],
                            journeys_already_done=resume_from)
            result: dict[str, Any] = {}
            if tag.lower().startswith("reresolve"):
                result = reresolve_all_skus(engine, tenant_id, actor_id=actor, commit="-commit" in tag.lower())
                run_id = result.get("runId")
            else:
                offset: int | None = resume_from
                run_id = previous["run_id"] if previous is not None else None
                while offset is not None:
                    result = backfill_sku_resolution_for_tenant(
                        engine, tenant_id=tenant_id, offset=offset, limit=10, run_id=run_id, actor_id=actor,
                        # A tag starting "unresolved" leaves every pinned SKU as it is.
                        only_without_sku=tag.lower().startswith("unresolved"),
                    )
                    run_id = result["runId"]
                    offset = result["nextOffset"]
            logger.info(
                "sku_run_finished", tenant_id=tenant_id, tag=tag, run_id=str(run_id),
                journeys=result.get("journeysTotal"), with_sku_before=result.get("withSkuBefore"),
                with_sku_after=result.get("withSkuAfter"), without_sku_after=result.get("withoutSkuAfter"),
                failed=len(result.get("journeysFailed") or []),
            )
        except CommitNotAllowed as refused:
            # Nothing was written and no run row was made, so the same tag can
            # be set again once the check is done.
            logger.warning(
                "sku_run_refused", tenant_id=tenant_id, tag=tag, code=refused.detail.get("code"),
                message=(
                    f"The SKU Apply run '{tag}' was NOT run for this Project and changed nothing: "
                    f"{refused.detail['message']} {check_numbers_text(refused.detail.get('check'))} "
                    "Set a 'reresolve-...' tag without '-commit' to run the check, review it, then set the "
                    "'-commit' tag again within 24 hours."
                ).strip(),
            )
        except Exception:
            logger.warning("sku_run_failed", tenant_id=tenant_id, tag=tag, exc_info=True)


def refresh_journey_dates(engine: Engine) -> None:
    """On every worker start: recompute each Journey's booking date from the
    receipts on file, and take back a gate-pass delivery date before the
    programme's floor. Idempotent. Each change is logged with the before and
    after and the receipts it was decided on (2026-10-20: Subrat Kumar
    Mallick showed "Booking 07 Oct 2026" while 21,000 was received on 05 Oct;
    the booking date was only recomputed when a Booking-stage receipt synced,
    so a receipt that came in another way never moved it)."""
    from audit_core.uc03_booking_confirmation_rules import (
        _minimum_booking_amount,
        evaluate_minimum_booking_payment,
    )
    from audit_core.uc03_p2_dates import programme_date_floor

    floor = programme_date_floor()
    for tenant_id in _active_tenants(engine):
        changed = 0
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                journeys = connection.execute(
                    text("SELECT DISTINCT journey_id FROM auditcore.payments WHERE tenant_id=:t"),
                    {"t": tenant_id},
                ).scalars().all()
                minimum = _minimum_booking_amount(connection, tenant_id=tenant_id)
            for journey_id in journeys:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    before = connection.execute(
                        text("SELECT booking_confirm_date FROM auditcore.journey_stage_states "
                             "WHERE tenant_id=:t AND journey_id=:j AND stage_code='BOOKING'"),
                        {"t": tenant_id, "j": journey_id},
                    ).scalar_one_or_none()
                    evaluate_minimum_booking_payment(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                     correlation_id="refresh-journey-dates")
                    after = connection.execute(
                        text("SELECT booking_confirm_date FROM auditcore.journey_stage_states "
                             "WHERE tenant_id=:t AND journey_id=:j AND stage_code='BOOKING'"),
                        {"t": tenant_id, "j": journey_id},
                    ).scalar_one_or_none()
                    if before != after:
                        changed += 1
                        receipts = connection.execute(
                            text(
                                """
                                SELECT p.receipt_date::text AS d, p.amount::text AS a, p.receipt_number AS n,
                                       COALESCE(e.association_status, 'NO_DOCUMENT') AS s
                                FROM auditcore.payments p
                                LEFT JOIN auditcore.evidence e
                                  ON e.tenant_id=p.tenant_id AND e.journey_id=p.journey_id
                                 AND e.di_document_id=p.source_di_document_id
                                WHERE p.tenant_id=:t AND p.journey_id=:j ORDER BY p.receipt_date
                                """
                            ),
                            {"t": tenant_id, "j": journey_id},
                        ).mappings().all()
                        logger.info(
                            "booking_date_refreshed", tenant_id=tenant_id, journey_id=str(journey_id),
                            before=str(before), after=str(after), minimum=str(minimum),
                            receipts=" | ".join(f"{r['d']} {r['a']} {r['n']} {r['s']}" for r in receipts),
                        )
            if floor is not None:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    cleared = connection.execute(
                        text(
                            """
                            UPDATE auditcore.deliveries
                            SET actual_delivered_at=NULL, updated_at_utc=now(), version_no=version_no+1
                            WHERE tenant_id=:t AND status_source='EVIDENCE'
                              AND actual_delivered_at < CAST(:floor AS date)
                            RETURNING journey_id, actual_delivered_at
                            """
                        ),
                        {"t": tenant_id, "floor": floor},
                    ).all()
                for journey_id, _ in cleared:
                    logger.info("delivery_date_before_floor_cleared", tenant_id=tenant_id, journey_id=str(journey_id))
            # Date reviews for documents still filed under an old DI key
            # (booking_docket / payment_receipt) were never raised.
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                legacy = connection.execute(
                    text("SELECT DISTINCT journey_id FROM auditcore.evidence WHERE tenant_id=:t "
                         "AND association_status='ACTIVE' AND document_type_key IN ('booking_docket','payment_receipt')"),
                    {"t": tenant_id},
                ).scalars().all()
            # The customer's name follows the KYC on file (2026-10-20).
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                all_journeys = connection.execute(
                    text("SELECT journey_id FROM auditcore.journeys WHERE tenant_id=:t"), {"t": tenant_id},
                ).scalars().all()
            renamed = 0
            for journey_id in all_journeys:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    if sync_customer_name(connection, tenant_id=tenant_id, journey_id=journey_id) == "VERIFIED":
                        renamed += 1
                        logger.info("customer_name_refreshed", tenant_id=tenant_id, journey_id=str(journey_id))
            logger.info("customer_names_refreshed", tenant_id=tenant_id, renamed=renamed)
            # Booking dates decide which side of a duplicate pair holds the
            # booking: re-check every pair once the dates are recomputed.
            from audit_core.uc03_duplicate_booking_detection import (
                sync_duplicate_booking_detection,
            )

            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                paired = connection.execute(
                    text("SELECT DISTINCT journey_id FROM auditcore.audit_findings WHERE tenant_id=:t "
                         "AND finding_type_code='DUPLICATE_BOOKING' AND finding_status IN ('OPEN','ACKNOWLEDGED') "
                         "UNION SELECT DISTINCT CAST(split_part(rule_key, ':', 2) AS uuid) "
                         "FROM auditcore.audit_findings WHERE tenant_id=:t "
                         "AND finding_type_code='DUPLICATE_BOOKING' AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
                    {"t": tenant_id},
                ).scalars().all()
            for journey_id in paired:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    sync_duplicate_booking_detection(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                     correlation_id="refresh-journey-dates")
            reviews_raised = 0
            for journey_id in legacy:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    outcome = sync_field_review_tasks(connection, tenant_id=tenant_id, journey_id=journey_id)
                reviews_raised += int((outcome or {}).get("RAISED", 0))
            logger.info("journey_dates_refreshed", tenant_id=tenant_id, journeys=len(journeys),
                        booking_dates_changed=changed, legacy_type_journeys=len(legacy),
                        date_reviews_raised=reviews_raised)
        except Exception:
            logger.warning("journey_dates_refresh_failed", tenant_id=tenant_id, exc_info=True)


def _one_shot_target(kind: str, engine: Engine, tag: str, reports: list[str]):
    """The function and arguments of a one-off job, by kind (the variable it was
    started by until 2026-10-27, without the AUDIT_CORE_ prefix and _TAG)."""
    if kind == "DIAG_REPORTS":
        from audit_core.uc03_p2_diagnostics import run_diagnostics
        return run_diagnostics, (engine, reports)
    if kind == "MASTER_RELOAD":
        from audit_core.oem_master_reload import run_master_reload
        return run_master_reload, (engine, tag)
    if kind == "SKU_RUN":
        return _sku_run_once, (engine, tag)
    if kind == "PEOPLE_NAMES":
        from audit_core.people_names import refresh_people_names
        return refresh_people_names, (engine,)
    if kind == "DUPLICATE_JOURNEY_PURGE":
        # Only the journey references listed in ``reports``, of the one tenant
        # the tag names (2026-10-31): see uc03_duplicate_journey_purge.
        from audit_core.uc03_duplicate_journey_purge import run_duplicate_journey_purge
        return run_duplicate_journey_purge, (engine, tag, reports)
    if kind == "FILE_IDENTITY_SCAN":
        # Whose each uploaded file is, for the one tenant the tag names
        # (2026-10-31): see uc03_file_identity_scan. Never deletes.
        from audit_core.uc03_file_identity_scan import run_file_identity_scan
        return run_file_identity_scan, (engine, tag, reports)
    if kind == "FILE_MOVE":
        # Files taken out of a booking go to their customer's own booking, as
        # planned in p2_file_moves (2026-11-01): see uc03_file_move. Never
        # changes or deletes the source file.
        from audit_core.uc03_file_move import run_file_move
        return run_file_move, (engine, tag, reports)
    if kind == "FILE_MOVE_RESEND":
        # The moved files' pages that failed on our side are sent again
        # (2026-11-01): see uc03_file_move.run_file_move_resend.
        from audit_core.uc03_file_move import run_file_move_resend
        return run_file_move_resend, (engine, tag, reports)
    if kind == "UNREAD_REREAD":
        # Documents whose read never finished are read again from their
        # stored pages (2026-11-01): see uc03_p2_unread_reread. A dry run lists them.
        from audit_core.uc03_p2_unread_reread import run_unread_reread
        return run_unread_reread, (engine, tag, _active_tenants(engine))
    if kind == "EARLIER_COPY_BACK":
        # The bookings holding a copy that comes back are recomputed
        # (2026-11-01): see uc03_p2_earlier_copy. A dry run lists them.
        from audit_core.uc03_p2_earlier_copy import run_earlier_copy_back
        return run_earlier_copy_back, (engine, tag, _active_tenants(engine))
    per_tenant = {
        "PAGE_RESEND": ("audit_core.uc03_p2_page_resend", "run_page_resend"),
        "INVOICE_RESCAN": ("audit_core.uc03_invoice_rescan", "run_invoice_rescan"),
        "ACCOUNT_STATEMENT": ("audit_core.uc03_account_statement_rebuild", "run_account_statement_rebuild"),
        "STATEMENT_REREAD": ("audit_core.uc03_statement_reread", "run_statement_reread"),
        "INVOICE_TASK_CLEANUP": ("audit_core.uc03_invoice_task_cleanup", "run_invoice_task_cleanup"),
        "VARIANCE_AUDIT": ("audit_core.uc03_variance_audit", "run_variance_audit"),
        "INSURANCE_CODE_REPAIR": ("audit_core.uc03_insurance_code_repair", "run_insurance_code_repair"),
        "FINANCE_RULE_REPAIR": ("audit_core.uc03_finance_rule_repair", "run_finance_rule_repair"),
        "INSURANCE_SOURCE_REVIEW": ("audit_core.uc03_insurance_source_review", "run_insurance_source_review"),
        "INSURANCE_HYPOTHECATION_REREAD": ("audit_core.uc03_insurance_hypothecation_reread",
                                           "run_insurance_hypothecation_reread"),
        "WAITING_CORRECTIONS": ("audit_core.uc03_waiting_corrections", "run_waiting_corrections"),
        # Cash transactions raise no tasks (product owner, 2026-10-31).
        "CASH_TASK_CLEANUP": ("audit_core.uc03_cash_task_cleanup", "run_cash_task_cleanup"),
        # Audit tasks that named no document, and documents the old name check
        # held out of their bookings (2026-11-01): see uc03_audit_task_cleanup.
        "AUDIT_TASK_CLEANUP": ("audit_core.uc03_audit_task_cleanup", "run_audit_task_cleanup"),
        # Bookings built again where a build would change them (2026-11-01).
        "BOOKING_REBUILD": ("audit_core.uc03_booking_rebuild", "run_booking_rebuild"),
        "DISCOUNT_VARIANCE_RECOMPUTE": ("audit_core.uc03_discount_variance_recompute",
                                        "run_discount_variance_recompute"),
        "ACCESSORY_INVOICE_REPAIR": ("audit_core.uc03_accessory_invoice_repair", "run_accessory_invoice_repair"),
    }
    if kind not in per_tenant:
        raise ValueError(f"unknown worker job kind {kind!r}")
    import importlib

    module, name = per_tenant[kind]
    return getattr(importlib.import_module(module), name), (engine, tag, _active_tenants(engine))


# The variables that start a one-off job when the worker starts (kept: a job
# can still be started this way, but that restarts the worker).
_ONE_SHOT_VARIABLES = {
    "MASTER_RELOAD": "AUDIT_CORE_MASTER_RELOAD_TAG",
    "PAGE_RESEND": "AUDIT_CORE_PAGE_RESEND_TAG",
    "INVOICE_RESCAN": "AUDIT_CORE_INVOICE_RESCAN_TAG",
    "ACCOUNT_STATEMENT": "AUDIT_CORE_ACCOUNT_STATEMENT_TAG",
    "STATEMENT_REREAD": "AUDIT_CORE_STATEMENT_REREAD_TAG",
    "INVOICE_TASK_CLEANUP": "AUDIT_CORE_INVOICE_TASK_CLEANUP_TAG",
    "VARIANCE_AUDIT": "AUDIT_CORE_VARIANCE_AUDIT_TAG",
    "SKU_RUN": "AUDIT_CORE_SKU_RUN_TAG",
}
_JOB_POLL_SECONDS = 60


def _run_job(engine: Engine, job: dict[str, Any]) -> None:
    job_id = str(job["job_id"])
    status, detail = "DONE", None
    try:
        target, args = _one_shot_target(job["job_kind"], engine, job["tag"], list(job["reports"] or []))
        logger.info("p2_worker_job_started", job_id=job_id, kind=job["job_kind"], tag=job["tag"])
        target(*args)
    except Exception as exc:  # recorded on the job row
        status, detail = "FAILED", f"{type(exc).__name__}: {exc}"[:1000]
        logger.warning("p2_worker_job_failed", job_id=job_id, kind=job["job_kind"], exc_info=True)
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        connection.execute(
            text("UPDATE auditcore.p2_worker_jobs SET status = :s, finished_at_utc = now(), detail = :d "
                 "WHERE job_id = CAST(:id AS uuid)"),
            {"s": status, "d": detail, "id": job_id},
        )
    logger.info("p2_worker_job_finished", job_id=job_id, kind=job["job_kind"], status=status)


def claim_worker_jobs(engine: Engine) -> list[dict[str, Any]]:
    """Take every pending one-off job (one worker gets each)."""
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        return [dict(r) for r in connection.execute(text("""
            UPDATE auditcore.p2_worker_jobs SET status = 'RUNNING', started_at_utc = now(), worker_id = :w
            WHERE job_id IN (SELECT job_id FROM auditcore.p2_worker_jobs WHERE status = 'PENDING'
                             ORDER BY requested_at_utc FOR UPDATE SKIP LOCKED LIMIT 5)
            RETURNING job_id, job_kind, tag, reports
        """), {"w": _WORKER_ID}).mappings().all()]


def _watch_worker_jobs(engine: Engine) -> None:
    """One-off jobs requested as rows of p2_worker_jobs (2026-10-27), so a job
    never needs a restart of the worker."""
    while not _stopping.is_set():
        try:
            for job in claim_worker_jobs(engine):
                threading.Thread(target=_run_job, args=(engine, job), name=f"job-{job['job_kind'].lower()}",
                                 daemon=True).start()
        except Exception:  # e.g. the table not migrated yet; try again later
            logger.warning("p2_worker_jobs_poll_failed", exc_info=True)
        _stopping.wait(_JOB_POLL_SECONDS)


_NAMES_REFRESH_SECONDS = 6 * 3600


def _refresh_names_every(engine: Engine) -> None:
    """People's names from Security, at start and every six hours (2026-10-27)."""
    from audit_core.people_names import refresh_people_names

    while not _stopping.is_set():
        refresh_people_names(engine)
        _stopping.wait(_NAMES_REFRESH_SECONDS)


def main() -> None:
    _configure_worker_observability()
    engine = get_engine()
    threading.Thread(target=refresh_journey_dates, args=(engine,), name="journey-dates", daemon=True).start()
    diag_reports = [r.strip() for r in os.environ.get("AUDIT_CORE_DIAG_REPORTS", "").split(",") if r.strip()]
    if diag_reports:
        target, args = _one_shot_target("DIAG_REPORTS", engine, "", diag_reports)
        threading.Thread(target=target, args=args, name="diagnostics", daemon=True).start()
    for kind, variable in _ONE_SHOT_VARIABLES.items():
        tag = os.environ.get(variable, "").strip()
        if tag:
            target, args = _one_shot_target(kind, engine, tag, [])
            threading.Thread(target=target, args=args, name=kind.lower().replace("_", "-"), daemon=True).start()
    threading.Thread(target=_watch_worker_jobs, args=(engine,), name="worker-jobs", daemon=True).start()
    threading.Thread(target=_refresh_names_every, args=(engine,), name="people-names", daemon=True).start()
    logger.info(
        "p2_worker_started",
        worker_id=_WORKER_ID,
        concurrency=_WORKER_CONCURRENCY,
        per_journey_concurrency=_PER_JOURNEY_CONCURRENCY,
    )
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    # Boot row and a beat a minute for the SuperAdmin System Health page (2026-10-27);
    # the stop stamp is written once run_forever has drained.
    heartbeat_stop = threading.Event()
    heartbeat = start_heartbeat_thread(engine, "verigence-p2-worker", heartbeat_stop)
    try:
        run_forever(engine)
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=10)


if __name__ == "__main__":
    main()
