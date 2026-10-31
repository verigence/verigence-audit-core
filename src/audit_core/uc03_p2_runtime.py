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
import os
import re
from typing import Any
from uuid import UUID

import structlog
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
      -- What the rows say, not when they were last written (2026-10-31, worker
      -- churn): the recompute rewrites payments, the vehicle, finance and
      -- insurance on every pass (updated_at_utc, version_no), so a recompute
      -- that changed nothing still changed a fingerprint built on timestamps
      -- and every control unit ran again after it. The row's content less
      -- its write stamps changes only when a value does.
      (SELECT concat_ws(':', COUNT(*),
                        md5(string_agg((to_jsonb(p) - 'updated_at_utc' - 'version_no')::text, ','
                                       ORDER BY p.payment_id)))
         FROM auditcore.payments p
        WHERE p.tenant_id=:tenant_id AND p.journey_id=:journey_id) AS payments,
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
      concat_ws(':',
        -- the vehicle and its SKU; when a check ran (decidedAtUtc) is not a fact
        (SELECT md5(string_agg((to_jsonb(jp) - 'updated_at_utc' - 'match_evidence')::text
                               || COALESCE((jp.match_evidence - 'decidedAtUtc')::text, ''), ','
                               ORDER BY jp.journey_product_id))
           FROM auditcore.journey_products jp
          WHERE jp.tenant_id=:tenant_id AND jp.journey_id=:journey_id),
        (SELECT md5(string_agg((to_jsonb(f) - 'updated_at_utc')::text, ',' ORDER BY f.finance_record_id))
           FROM auditcore.finance_records f
          WHERE f.tenant_id=:tenant_id AND f.journey_id=:journey_id),
        (SELECT pricing_effective_on FROM auditcore.bookings b
          WHERE b.tenant_id=:tenant_id AND b.journey_id=:journey_id),
        (SELECT COUNT(*) FROM auditcore.p2_vehicle_identifications vi
          WHERE vi.tenant_id=:tenant_id AND vi.journey_id=:journey_id),
        -- the PC's Self / Inhouse answer (2026-10-28: an answer that changed
        -- no fact left the checks skipped) moves insurance_source_set_at_utc
        (SELECT md5(string_agg((to_jsonb(ir) - 'updated_at_utc' - 'version_no')::text, ','
                               ORDER BY ir.insurance_record_id))
           FROM auditcore.insurance_records ir
          WHERE ir.tenant_id=:tenant_id AND ir.journey_id=:journey_id)) AS deal
    """
)


def fact_fingerprint(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str:
    row = connection.execute(
        _FINGERPRINT_SQL, {"tenant_id": tenant_id, "journey_id": journey_id}
    ).mappings().one()
    material = "|".join(str(row[key] or "") for key in ("facts", "payments", "evidence", "legacy_review", "photos", "deal"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# Set for the rest of a transaction whose writes follow from facts it already
# read (the worker's recompute and its control units): the task a recompute
# closes because the PC confirmed a field is not a new fact. The MANUAL_
# VERIFICATION_REVIEW task trigger (migration 0180) then asks for no second
# recompute -- before 2026-10-31 every such close asked for one, silently,
# and the recompute could not store its fingerprint, so the fact sweep asked
# for a third (FIELD_CORRECTED v51 -> silent v52 -> FACT_SWEEP v53).
DERIVED_WRITES_SETTING = "audit_core.p2_derived_writes"


def mark_derived_writes(connection: Connection) -> None:
    """The rest of this transaction only writes what follows from facts it
    has read: a task it raises or closes asks for no recompute of its own."""
    connection.execute(text("SELECT set_config(:name, 'on', true)"), {"name": DERIVED_WRITES_SETTING})


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
            -- A forced request still waiting stays forced when an ordinary
            -- request for the same work arrives (2026-10-28: a PC's "done"
            -- asked for a forced re-check, a fact change a second later
            -- overwrote it, and the check was skipped as unchanged -- the
            -- task sat in VERIFYING).
            DO UPDATE SET payload=CASE
                            WHEN auditcore.p2_work_queue.work_status IN ('PENDING','RETRY_WAIT')
                              AND auditcore.p2_work_queue.payload->>'force' = 'true'
                              THEN EXCLUDED.payload || '{"force": true}'::jsonb
                            ELSE EXCLUDED.payload
                          END,
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
                          -- an item already waiting keeps its place; one that
                          -- had finished joins the back of the line; one
                          -- running joins it when it finishes (_complete)
                          queued_at_utc=CASE
                            WHEN auditcore.p2_work_queue.work_status
                                 IN ('PENDING','RETRY_WAIT','CLAIMED','PROCESSING')
                              THEN auditcore.p2_work_queue.queued_at_utc
                            ELSE now()
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


_runtime_logger = structlog.get_logger(__name__)


def hhmm(value: str, default: str) -> str:
    """``HH:MM`` from an environment variable; anything that is not a clock
    time falls back to ``default`` (a typo must not stop the worker)."""
    hour, sep, minute = value.strip().partition(":")
    if sep and hour.isdigit() and minute.isdigit() and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59:
        return f"{int(hour):02d}:{int(minute):02d}"
    _runtime_logger.warning("p2_dayend_sweep_time_invalid", value=value, using=default)
    return default


# When (UTC, HH:MM) the day-end sweep runs (product owner 2026-10-22, moved
# the same day): 02:00 IST = 20:30 UTC, the evening of the previous UTC day.
# P2_DAYEND_SWEEP_UTC overrides it. The PC's messages quote this time in IST.
DAYEND_SWEEP_DEFAULT_UTC = "20:30"
DAYEND_SWEEP_UTC = hhmm(os.environ.get("P2_DAYEND_SWEEP_UTC", DAYEND_SWEEP_DEFAULT_UTC), DAYEND_SWEEP_DEFAULT_UTC)


def dayend_sweep_ist() -> str:
    """The configured sweep time as the PC reads it: ``HH:MM IST``."""
    hour, _, minute = DAYEND_SWEEP_UTC.partition(":")
    total = (int(hour) * 60 + int(minute) + 330) % 1440
    return f"{total // 60:02d}:{total % 60:02d} IST"


def dayend_removal_notice(pages: int = 1) -> str:
    """What the PC is told about a page refused for image quality: the system
    clears it at the day-end clean-up, and a clearer copy is still needed."""
    if pages == 1:
        return (f"This page will be removed by the system at the day-end clean-up ({dayend_sweep_ist()}). "
                "Please upload a clearer copy.")
    return (f"These pages will be removed by the system at the day-end clean-up ({dayend_sweep_ist()}). "
            "Please upload clearer copies.")


# A file with more pages than this gets the PC a "review this upload" notice
# (product owner 2026-10-26): are all these pages really needed? It is advice,
# never a blocker: the upload goes through, the notice is outside the delivery
# gate, and the Daily Processing report highlights the file. Set
# P2_LARGE_UPLOAD_PAGES to change it.
LARGE_UPLOAD_PAGES = int(os.environ.get("P2_LARGE_UPLOAD_PAGES", "25"))


# What the PC does to close an image-quality task (product owner 2026-10-22):
# the system cannot know which upload answers it, so he closes it by hand with
# a comment. These two phrases are the guidance, quoted exactly; they are not a
# fixed list: any non-blank comment closes the task.
QUALITY_CLOSE_INSTRUCTION = (
    'Once uploaded, come back and close the task with the comment "Document Uploaded". '
    'If the document does not need to be uploaded again, close the task with the comment '
    '"Document Not required to be uploaded for Extraction".'
)


# The day-end sweep (product owner 2026-10-22) clears the pages that ended
# FAILED from the Journey: the unit goes CANCELLED (hidden everywhere, as a
# removed page is) and a PAGE_SWEPT event is recorded. Unlike a page a person
# removes, a swept page keeps its failure code and reason, and it still counts
# as a failed page of its file -- the file's "Document to resubmit" task stays
# open, the failure report still lists it -- until it is replaced by a
# re-upload (last_error then holds SWEPT_RESOLVED_CODES) or removed by a
# person (a PAGE_REMOVED event for the unit; its failure code is kept, as for
# any removed page). No column marks it: the events do.
PAGE_SWEPT_EVENT = "PAGE_SWEPT"
PAGE_REMOVED_EVENT = "PAGE_REMOVED"
SWEPT_RESOLVED_CODES = ("SUPERSEDED_PAGE",)


def swept_pending_sql(alias: str = "q") -> str:
    """SQL condition on a p2_document_queue row (``alias``): swept by the
    day-end sweep and still waiting to be resubmitted."""
    resolved = ",".join(f"'{code}'" for code in SWEPT_RESOLVED_CODES)
    return (
        f"({alias}.queue_status='CANCELLED' AND {alias}.last_error IS NOT NULL "
        f"AND {alias}.last_error NOT IN ({resolved}) "
        f"AND EXISTS (SELECT 1 FROM auditcore.p2_activity_events sw "
        f"WHERE sw.tenant_id={alias}.tenant_id AND sw.journey_id={alias}.journey_id "
        f"AND sw.event_type='{PAGE_SWEPT_EVENT}' AND sw.subject_id={alias}.queue_id::text) "
        f"AND NOT EXISTS (SELECT 1 FROM auditcore.p2_activity_events rm "
        f"WHERE rm.tenant_id={alias}.tenant_id AND rm.journey_id={alias}.journey_id "
        f"AND rm.event_type='{PAGE_REMOVED_EVENT}' AND rm.subject_id={alias}.queue_id::text))"
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
    received is sent straight away. ``marker`` is ``r`` for a PC's Retry (the
    nightly sweep ``n`` was withdrawn on 2026-10-22: nothing is recovered or
    read again at night). Returns the client upload id in force."""
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
    # The page's 30 minutes start again (product owner 2026-10-21).
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status='QUEUED', status_reason=NULL, last_error=NULL,
                read_clock_started_at_utc=now(), updated_at_utc=now()
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


def claim_scheduled_run(
    connection: Connection, *, tenant_id: str, run_name: str, run_key: str, claimed_by: str | None = None,
) -> bool:
    """Claim a once-only run (the nightly review of one night, a one-time
    deploy step) for a tenant, durably (2026-10-20 review: a worker restart
    must not run the night again). True when this caller claimed it; the
    claim commits or rolls back with the caller's transaction."""
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_scheduled_runs (tenant_id, run_name, run_key, claimed_by)
            VALUES (:tenant_id, :run_name, :run_key, :claimed_by)
            ON CONFLICT (tenant_id, run_name, run_key) DO NOTHING
            RETURNING run_key
            """
        ),
        {"tenant_id": tenant_id, "run_name": run_name, "run_key": run_key, "claimed_by": claimed_by},
    ).scalar_one_or_none() is not None


def request_page_reread(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    queue_id: UUID,
    requested_by: str,
    correlation_id: str | None,
) -> None:
    """Ask Document Intelligence to read a page it already holds once more
    (decision 2026-10-01: never a re-upload for a page DI has). Queued work:
    a second request for the same page coalesces, and DI itself never
    doubles a reading already in hand. A page DI never received is sent
    through the ordinary recovery instead."""
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
        request_page_recovery(
            connection, tenant_id=tenant_id, journey_id=journey_id, queue_id=queue_id,
            marker="r", requested_by=requested_by, correlation_id=correlation_id,
        )
        return
    # A person asked for it: the page's 30 minutes start again (2026-10-21),
    # and a failed page is back in line (QUEUED) so the reading may settle
    # it; work still running for the page never writes over FAILED.
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET read_clock_started_at_utc=now(),
                queue_status=CASE WHEN queue_status IN ('FAILED','DEAD_LETTER') THEN 'QUEUED' ELSE queue_status END,
                updated_at_utc=CASE WHEN queue_status IN ('FAILED','DEAD_LETTER') THEN now() ELSE updated_at_utc END
            WHERE tenant_id=:tenant_id AND queue_id=:queue_id
            """
        ),
        {"tenant_id": tenant_id, "queue_id": queue_id},
    )
    enqueue_work(
        connection, tenant_id=tenant_id, journey_id=journey_id, work_type="PAGE_REREAD",
        work_key=str(queue_id), payload={"queueId": str(queue_id), "uploadedBy": requested_by},
        correlation_id=correlation_id,
    )
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="PAGE_REREAD_REQUESTED",
        subject_type="DOCUMENT_PAGE", subject_id=str(queue_id), details={"by": requested_by},
        correlation_id=correlation_id,
    )


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
    attempts apart in the client upload id: ``r`` for a PC's Retry. Returns
    the new client upload id."""
    base = client_upload_id.split("~", 1)[0]
    # The next number is one past the highest already used for this page and
    # marker -- never a count of rows: the queue row carries its own id, so a
    # count stays at 1 from the second attempt on and re-used "~r2", which DI
    # refuses as a conflict (RJ Hot max p11, 2026-10-21).
    used = connection.execute(
        text("SELECT client_upload_id FROM auditcore.p2_document_queue WHERE tenant_id=:t AND client_upload_id LIKE :p"),
        {"t": tenant_id, "p": f"{base}~{marker}%"},
    ).scalars().all()
    numbers = [int(match[1]) for value in [*used, client_upload_id]
               if (match := re.fullmatch(rf"{re.escape(base)}~{re.escape(marker)}(\d+)", str(value)))]
    new_id = f"{base}~{marker}{max(numbers, default=0) + 1}"
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status='QUEUED', client_upload_id=:client_upload_id, di_document_id=NULL,
                di_state=NULL, di_processing_status=NULL, di_submitted_at_utc=NULL,
                di_processed_seen_at_utc=NULL, status_reason=NULL, last_error=NULL,
                attempt_count=0, extracted_field_count=0, read_clock_started_at_utc=now(),
                updated_at_utc=now()
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
