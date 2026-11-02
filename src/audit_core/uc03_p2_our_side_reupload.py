"""Files that failed on our side are uploaded again from what we stored (product owner 2026-11-02).

"The cases which failed on our side, why can't they be uploaded again?" They
can: the PC's file is in storage. Two kinds were left asking the PC to upload
it again:

- a file never split into pages within its 30 minutes: "The file did not
  finish uploading within 30 minutes" (the browser never confirmed the
  upload) or "The file was not split into pages within 30 minutes";
- a file some of whose pages, or documents put together from its pages, were
  not read within their 30 minutes (NOT_READ_IN_30_MIN, 23 to 26 Oct) or
  could not be put together (GROUPING_FAILED). The day-end sweep took them
  off the booking, and "Send again" only resends a page whose type is known,
  which a page never read has not got.

This job does what the PC's task asks the PC to do: upload the whole file
again. The stored original is copied, byte for byte, to a new upload of the
same booking (kept per outlet and day, under the customer's name,
uc03_p2_object_keys) and split like any upload; the split then does what it
does for a file uploaded again (uc03_p2_worker._split_batch): a page already
read on the booking is not sent again, a failed or swept page of the same scan
is replaced -- its task closes -- and the rest is read as a fresh upload,
pages put together into documents as usual. A file never split is closed as
sent again, so its "upload the file again" task closes. The new upload is
the PC's (uploaded_by), and its history says Verigence sent it again.

A file is sent again only when all of these hold:
- the booking is open (not cancelled, a duplicate, closed without delivery,
  or review-complete) and its documents are not marked reviewed;
- the file is the booking's own (not taken out as another customer's);
- the PC has not closed the file's task by hand, nor uploaded the same file
  again since (same name or size, read or still being read);
- the stored original is there, at the size the browser announced (a file
  that never reached storage stays with the PC: nothing can be sent).

p2_worker_jobs kind OUR_SIDE_REUPLOAD, tag ``<tenant_id>:<label>``: a dry run
unless the label ends with "-commit"; ``reports`` may list journey references
to limit it to. At most _PER_RUN files are sent per run, oldest upload first,
so the reader is never flooded -- run it again for the rest. What it found and
did is returned (stored on the job row) and logged per file.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

_JOB = "OUR_SIDE_REUPLOAD"
_PER_RUN = int(os.environ.get("AUDIT_CORE_OUR_SIDE_REUPLOAD_PER_RUN", "8"))
# Pages and documents that failed on our side, not the page's.
OUR_SIDE_CODES = ("NOT_READ_IN_30_MIN", "GROUPING_FAILED")
# A person closed the task: nothing is sent again (2026-10-27).
_PERSON_CLOSED = ("CLOSED_BY_PC", "EXCEPTION_ACCEPTED")
_SENT_AGAIN_REASON = ("Sent again by Verigence from the stored file on {day} (it failed on our side, "
                      "not because of the file).")


def parse_tag(tag: str) -> tuple[str, bool]:
    tenant_id, sep, label = (tag or "").strip().partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{_JOB} needs the tag '<tenant_id>:<label>' (label ending -commit to send)")
    return tenant_id.strip(), label.strip().endswith("-commit")


def new_batch_id(batch_id: UUID | str) -> UUID:
    """The id of the upload made from ``batch_id``: fixed, so a second run finds it."""
    return uuid5(NAMESPACE_URL, f"verigence:p2-our-side-reupload:{batch_id}")


def candidates(connection: Connection, tenant_id: str, references: list[str] | None = None) -> list[dict[str, Any]]:
    """The booking's own files that failed on our side and still wait for the PC."""
    from audit_core.uc03_p2_page_removal import _OPEN_JOURNEY
    from audit_core.uc03_p2_runtime import swept_pending_sql
    from audit_core.uc03_p2_task_producer import (
        FILE_NOT_SPLIT_IN_TIME_REASON,
        FILE_NOT_UPLOADED_IN_TIME_REASON,
    )

    rows = connection.execute(
        text(
            f"""
            SELECT b.batch_id, b.journey_id, j.journey_reference, b.original_filename, b.content_type,
                   b.size_bytes, b.page_count, b.batch_status, b.failure_reason, b.original_object_key,
                   b.uploaded_by_actor_id, b.created_at_utc,
                   (b.batch_status='FAILED' AND COALESCE(b.page_count, 0)=0) AS never_split,
                   (SELECT COUNT(*) FROM auditcore.p2_document_queue q
                     WHERE q.tenant_id=b.tenant_id AND q.batch_id=b.batch_id
                       AND q.last_error = ANY(:codes)
                       AND (q.queue_status IN ('FAILED','DEAD_LETTER') OR {swept_pending_sql("q")})) AS units_failed,
                   (SELECT COALESCE(SUM(COALESCE(cardinality(q.page_numbers), 1)), 0)
                      FROM auditcore.p2_document_queue q
                     WHERE q.tenant_id=b.tenant_id AND q.batch_id=b.batch_id AND q.unit_kind='PAGE'
                       AND q.last_error = ANY(:codes)
                       AND (q.queue_status IN ('FAILED','DEAD_LETTER') OR {swept_pending_sql("q")})) AS pages_failed,
                   EXISTS (SELECT 1 FROM auditcore.p2_stage_document_reviews rv
                           WHERE rv.tenant_id=b.tenant_id AND rv.journey_id=b.journey_id) AS reviewed,
                   (SELECT t.title FROM auditcore.p2_tasks t
                     WHERE t.tenant_id=b.tenant_id AND t.journey_id=b.journey_id
                       AND t.dedupe_key = 'resubmit:' || b.journey_id::text || ':' || b.batch_id::text
                       AND t.task_status IN ('VERIFIED_COMPLETE','CANCELLED')
                       AND t.completion_result->>'outcome' = ANY(:person_closed)
                     ORDER BY t.updated_at_utc DESC LIMIT 1) AS closed_by_person,
                   (SELECT o.original_filename FROM auditcore.p2_upload_batches o
                     WHERE o.tenant_id=b.tenant_id AND o.journey_id=b.journey_id AND o.batch_id<>b.batch_id
                       AND o.created_at_utc > b.created_at_utc
                       AND o.batch_status NOT IN ('FAILED','CANCELLED','AWAITING_UPLOAD')
                       AND (o.original_filename=b.original_filename OR o.size_bytes=b.size_bytes)
                     ORDER BY o.created_at_utc LIMIT 1) AS uploaded_again_as,
                   (SELECT COUNT(*) FROM auditcore.p2_document_queue q
                     WHERE q.tenant_id=b.tenant_id AND q.batch_id=b.batch_id AND q.unit_kind='PAGE'
                       AND q.last_error LIKE 'DI_QUALITY%') AS quality_pages
            FROM auditcore.p2_upload_batches b
            JOIN auditcore.journeys j ON j.tenant_id=b.tenant_id AND j.journey_id=b.journey_id
            WHERE b.tenant_id=:t
              AND {_OPEN_JOURNEY}
              AND b.identity_enforced_at_utc IS NULL AND COALESCE(b.identity_status, '') <> 'FOREIGN'
              AND COALESCE(b.client_upload_id, '') NOT LIKE 'our-side-%'
              AND (cardinality(CAST(:refs AS text[])) = 0 OR j.journey_reference = ANY(CAST(:refs AS text[])))
              AND (
                (b.batch_status='FAILED' AND COALESCE(b.page_count, 0)=0
                 AND b.failure_reason IN (:not_uploaded, :not_split))
                OR EXISTS (SELECT 1 FROM auditcore.p2_document_queue q
                           WHERE q.tenant_id=b.tenant_id AND q.batch_id=b.batch_id
                             AND q.last_error = ANY(:codes)
                             AND (q.queue_status IN ('FAILED','DEAD_LETTER') OR {swept_pending_sql("q")}))
              )
            ORDER BY b.created_at_utc, b.batch_id
            """
        ),
        {"t": tenant_id, "codes": list(OUR_SIDE_CODES), "person_closed": list(_PERSON_CLOSED),
         "refs": list(references or []), "not_uploaded": FILE_NOT_UPLOADED_IN_TIME_REASON,
         "not_split": FILE_NOT_SPLIT_IN_TIME_REASON},
    ).mappings().all()
    return [dict(r) for r in rows]


def why_not(connection: Connection, tenant_id: str, row: dict[str, Any]) -> str | None:
    """Why this file is not sent again, or None."""
    if row["reviewed"]:
        return "the booking's documents are marked reviewed"
    if row["closed_by_person"]:
        return f'the PC closed its task "{row["closed_by_person"]}"'
    if row["uploaded_again_as"]:
        return f"the PC uploaded it again ({row['uploaded_again_as']})"
    if not row["original_object_key"]:
        return "no stored file is recorded"
    sent = connection.execute(
        text("SELECT 1 FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND batch_id=:b"),
        {"t": tenant_id, "b": new_batch_id(row["batch_id"])},
    ).first()
    if sent is not None:
        return "already sent again"
    return None


def stored_size(row: dict[str, Any]) -> tuple[int | None, str | None]:
    """The stored original's size, or why it cannot be used."""
    from audit_core.uc03_p2_storage import (
        P2DocumentStorageError,
        get_p2_document_storage,
    )

    try:
        meta = get_p2_document_storage().head_object(str(row["original_object_key"]))
    except (RuntimeError, P2DocumentStorageError):
        return None, "the file never reached storage: the PC has to upload it again"
    size = int(meta.get("contentLength") or 0)
    expected = int(row["size_bytes"] or 0)
    if size <= 0 or (expected and size != expected):
        return size, f"the stored file is incomplete ({size} of {expected} bytes): the PC has to upload it again"
    return size, None


def _send(engine: Engine, tenant_id: str, row: dict[str, Any], *, tag: str, now: datetime) -> UUID:
    """The stored original as a new upload of the same booking, split like any upload."""
    from audit_core.uc03_p2_object_keys import ist_day, original_key, upload_labels
    from audit_core.uc03_p2_runtime import enqueue_work, record_activity
    from audit_core.uc03_p2_storage import get_p2_document_storage
    from audit_core.uc03_p2_task_producer import sync_processing_failure_tasks

    batch_id = new_batch_id(row["batch_id"])
    journey_id = UUID(str(row["journey_id"]))
    storage = get_p2_document_storage()
    payload = storage.get_object(str(row["original_object_key"]))
    expected = int(row["size_bytes"] or 0)
    if expected and len(payload) != expected:
        raise RuntimeError(f"stored file is {len(payload)} bytes, {expected} expected")
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        labels = upload_labels(connection, tenant_id=tenant_id, journey_id=journey_id)
    content_type = str(row["content_type"] or "application/pdf")
    key = original_key(tenant_id, journey_id, batch_id, filename=str(row["original_filename"]),
                       outlet_name=labels[0], pc_name=labels[1], customer_name=labels[2], now=now)
    # Object storage outside any transaction; a retry writes the same key.
    storage.put_object(key, payload, content_type=content_type)
    correlation = f"p2-our-side-reupload:{row['batch_id']}"
    day = ist_day(now).strftime("%d %b %Y")
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        inserted = connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, client_upload_id,
                    original_filename, content_type, size_bytes, page_count,
                    original_object_key, batch_status, uploaded_by_actor_id, correlation_id
                ) VALUES (
                    :t, :b, :j, :client, :name, :ct, :size, 0, :key, 'UPLOADED', :by, :correlation
                )
                ON CONFLICT DO NOTHING
                RETURNING batch_id
                """
            ),
            {"t": tenant_id, "b": batch_id, "j": journey_id, "client": f"our-side-{row['batch_id']}",
             "name": row["original_filename"], "ct": content_type, "size": len(payload), "key": key,
             "by": row["uploaded_by_actor_id"], "correlation": correlation},
        ).scalar_one_or_none()
        if inserted is None:
            return batch_id
        enqueue_work(
            connection, tenant_id=tenant_id, journey_id=journey_id, work_type="SPLIT_BATCH", work_key=str(batch_id),
            payload={"batchId": str(batch_id), "uploadedBy": str(row["uploaded_by_actor_id"]),
                     "uploadedByRole": "PC"},
            correlation_id=correlation,
        )
        record_activity(
            connection, tenant_id=tenant_id, journey_id=journey_id, event_type="UPLOAD_ACCEPTED",
            subject_type="UPLOAD_BATCH", subject_id=str(batch_id),
            details={"filename": row["original_filename"], "sizeBytes": len(payload), "by": "Verigence",
                     "sentAgainFrom": str(row["batch_id"]), "originallyUploadedBy": str(row["uploaded_by_actor_id"]),
                     "why": "It failed on our side; sent again from the stored file", "job": tag},
            correlation_id=correlation,
        )
        if row["never_split"]:
            # Nothing of it was ever split: it is closed as sent again, so its
            # "upload the file again" task closes; the new upload carries on.
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_upload_batches
                    SET batch_status='CANCELLED', failure_reason=:why, updated_at_utc=now()
                    WHERE tenant_id=:t AND batch_id=:b AND batch_status='FAILED' AND COALESCE(page_count, 0)=0
                    """
                ),
                {"t": tenant_id, "b": row["batch_id"], "why": _SENT_AGAIN_REASON.format(day=day)},
            )
            record_activity(
                connection, tenant_id=tenant_id, journey_id=journey_id, event_type="UPLOAD_SENT_AGAIN",
                subject_type="UPLOAD_BATCH", subject_id=str(row["batch_id"]),
                details={"filename": row["original_filename"], "newBatchId": str(batch_id), "by": "Verigence",
                         "failureReason": row["failure_reason"], "job": tag},
                correlation_id=correlation,
            )
            sync_processing_failure_tasks(connection, tenant_id=tenant_id, journey_id=journey_id)
    return batch_id


def run_our_side_reupload(engine: Engine, tag: str, references: list[str] | None = None) -> dict[str, Any]:
    tenant_id, commit = parse_tag(tag)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = candidates(connection, tenant_id, references)
    totals = {"files": len(rows), "would_send": 0, "sent": 0, "skipped": 0, "not_in_storage": 0,
              "left_for_next_run": 0, "failed": 0}
    items: list[dict[str, Any]] = []
    for row in rows:
        item = {"reference": row["journey_reference"], "file": row["original_filename"],
                "batchId": str(row["batch_id"]), "sizeBytes": row["size_bytes"],
                "neverSplit": bool(row["never_split"]), "pagesFailed": int(row["pages_failed"] or 0),
                "unitsFailed": int(row["units_failed"] or 0), "qualityPages": int(row["quality_pages"] or 0),
                "failure": row["failure_reason"]}
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                why = why_not(connection, tenant_id, row)
            outcome = "SKIPPED" if why else None
            if outcome is None:
                size, why = stored_size(row)
                item["storedBytes"] = size
                outcome = "NOT_IN_STORAGE" if why else None
            if outcome is None:
                if not commit:
                    outcome = "WOULD_SEND"
                elif totals["sent"] >= _PER_RUN:
                    outcome, why = "NEXT_RUN", f"more than {_PER_RUN} in one run"
                else:
                    item["newBatchId"] = str(_send(engine, tenant_id, row, tag=tag, now=now))
                    outcome = "SENT"
        except Exception as exc:  # one file never stops the others
            outcome, why = "FAILED", f"{type(exc).__name__}: {exc}"[:300]
            logger.warning("our_side_reupload_item_failed", tag=tag, batch_id=str(row["batch_id"]), exc_info=True)
        item["outcome"], item["why"] = outcome, why
        totals[{"WOULD_SEND": "would_send", "SENT": "sent", "SKIPPED": "skipped", "NOT_IN_STORAGE": "not_in_storage",
                "NEXT_RUN": "left_for_next_run", "FAILED": "failed"}[outcome]] += 1
        items.append(item)
        logger.info("our_side_reupload_item", tag=tag, tenant_id=tenant_id, commit=commit, **item)
    logger.info("our_side_reupload_finished", tag=tag, tenant_id=tenant_id, commit=commit, per_run=_PER_RUN, **totals)
    return {"commit": commit, "perRun": _PER_RUN, **totals, "items": items}
