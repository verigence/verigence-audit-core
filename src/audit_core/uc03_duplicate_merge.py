"""DUPLICATE_MERGE: the documents only a duplicate copy holds go to the booking that is kept (2026-11-02).

Four same-customer duplicate pairs on Mahindra Orissa could not be closed:
the copy to be deleted held a document the booking kept did not (a PAN, the
KYC form, the DMS invoice and cost sheet, the booking form), so deleting the
copy would lose it, and the PCs had not uploaded it to the kept booking.
Product owner, 2 Nov: go ahead -- move the document, then delete the copy.

What it does, for each ``reports`` entry ``<KEEP_REF>><COPY_REF>:<type>[,<type>...]``
- every document of those types ACTIVE on the copy is found with the unit
  that read it (a page, or a document put together from pages);
- that unit's stored file (the page, or the merged document) is copied,
  byte for byte, to a new upload of the kept booking (per outlet, per day,
  under the customer's name: uc03_p2_object_keys), uploaded by the kept
  booking's PC, and split and read there like any upload;
- both bookings' histories say what was copied from where.
The copy itself, its documents and its files are not changed here: it is
deleted afterwards with DUPLICATE_JOURNEY_PURGE, once the documents are read
on the kept booking.

Safeguards
- Only a document type the kept booking does not hold ACTIVE is copied (a
  type it already holds is skipped and reported), so nothing on the kept
  booking is replaced.
- Neither booking may be cancelled, a duplicate or review-complete.
- Idempotent: the new upload's id is fixed per copied unit, so a second run
  finds it and copies nothing again.
- Dry run unless the tag (``<tenant_id>:<label>``) ends with "-commit". What
  it found and did is returned (kept on the job row) and logged.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

_JOB = "DUPLICATE_MERGE"
_CLOSED = ("BOOKING_CANCELLED", "DUPLICATE_BOOKING")


def parse_tag(tag: str) -> tuple[str, bool]:
    tenant_id, sep, label = (tag or "").strip().partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{_JOB} needs the tag '<tenant_id>:<label>' (label ending -commit to copy)")
    return tenant_id.strip(), label.strip().endswith("-commit")


def parse_entry(entry: str) -> tuple[str, str, list[str]]:
    """``KEEP>COPY:type,type`` -> (keep, copy, types)."""
    pair, sep, types = (entry or "").partition(":")
    keep, arrow, copy = pair.partition(">")
    kinds = [t.strip() for t in types.split(",") if t.strip()]
    if not sep or not arrow or not keep.strip() or not copy.strip() or not kinds:
        raise ValueError(f"{_JOB} entry {entry!r} is not 'KEEP_REF>COPY_REF:type[,type]'")
    return keep.strip(), copy.strip(), kinds


def new_batch_id(queue_id: UUID | str, keep_journey_id: UUID | str) -> UUID:
    return uuid5(NAMESPACE_URL, f"verigence:p2-duplicate-merge:{queue_id}:{keep_journey_id}")


def _journey(connection: Connection, tenant_id: str, reference: str) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT j.journey_id, j.journey_reference, j.created_by_actor_id, j.review_completed_at_utc,
                   EXISTS (SELECT 1 FROM auditcore.journey_stage_states s
                           WHERE s.tenant_id=j.tenant_id AND s.journey_id=j.journey_id AND s.stage_code='BOOKING'
                             AND s.business_status = ANY(:closed)) AS closed
            FROM auditcore.journeys j WHERE j.tenant_id=:t AND j.journey_reference=:r
            """
        ),
        {"t": tenant_id, "r": reference, "closed": list(_CLOSED)},
    ).mappings().one_or_none()
    return dict(row) if row else None


def _units(connection: Connection, tenant_id: str, copy_id: UUID, kind: str) -> list[dict[str, Any]]:
    """The units that read each ACTIVE document of ``kind`` on the copy."""
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (q.queue_id) e.evidence_id, e.document_type_key, q.queue_id, q.unit_kind,
                   q.page_number, q.page_numbers, q.page_object_key, b.original_filename, b.content_type
            FROM auditcore.evidence e
            JOIN auditcore.p2_document_queue q
              ON q.tenant_id=e.tenant_id AND q.journey_id=e.journey_id AND q.di_document_id=e.di_document_id
            JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='ACTIVE' AND e.document_type_key=:k
              AND q.queue_status IN ('READY','NEEDS_REVIEW','SUPPORTING') AND q.page_object_key IS NOT NULL
            ORDER BY q.queue_id, q.updated_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": copy_id, "k": kind},
    ).mappings().all()
    return [dict(r) for r in rows]


def _holds(connection: Connection, tenant_id: str, journey_id: UUID, kind: str) -> bool:
    return connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j "
             "AND association_status='ACTIVE' AND document_type_key=:k)"),
        {"t": tenant_id, "j": journey_id, "k": kind},
    ).scalar_one()


def _copy_unit(engine: Engine, tenant_id: str, keep: dict[str, Any], copy: dict[str, Any], unit: dict[str, Any],
               *, tag: str, now: datetime) -> UUID:
    from audit_core.uc03_p2_object_keys import original_key, upload_labels
    from audit_core.uc03_p2_runtime import enqueue_work, record_activity
    from audit_core.uc03_p2_storage import get_p2_document_storage

    batch_id = new_batch_id(unit["queue_id"], keep["journey_id"])
    storage = get_p2_document_storage()
    payload = storage.get_object(str(unit["page_object_key"]))
    if not payload:
        raise RuntimeError(f"stored file {unit['page_object_key']} is empty")
    pages = [int(p) for p in (unit["page_numbers"] or [unit["page_number"]])]
    is_pdf = str(unit["page_object_key"]).endswith(".pdf") or payload[:5] == b"%PDF-"
    stem = str(unit["original_filename"] or "document").rsplit(".", 1)[0]
    filename = (f"{unit['document_type_key']} from {copy['journey_reference']} - {stem} "
                f"page{'s' if len(pages) > 1 else ''} {'-'.join(map(str, pages))}"
                + (".pdf" if is_pdf else ""))
    content_type = "application/pdf" if is_pdf else str(unit["content_type"] or "application/octet-stream")
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        labels = upload_labels(connection, tenant_id=tenant_id, journey_id=keep["journey_id"])
    key = original_key(tenant_id, keep["journey_id"], batch_id, filename=filename, outlet_name=labels[0],
                       pc_name=labels[1], customer_name=labels[2], now=now)
    # Object storage outside any transaction; a retry writes the same key.
    storage.put_object(key, payload, content_type=content_type)
    correlation = f"p2-duplicate-merge:{unit['queue_id']}"
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
            {"t": tenant_id, "b": batch_id, "j": keep["journey_id"], "client": f"dup-merge-{unit['queue_id']}",
             "name": filename[:250], "ct": content_type, "size": len(payload), "key": key,
             "by": keep["created_by_actor_id"], "correlation": correlation},
        ).scalar_one_or_none()
        if inserted is None:
            return batch_id
        enqueue_work(
            connection, tenant_id=tenant_id, journey_id=keep["journey_id"], work_type="SPLIT_BATCH",
            work_key=str(batch_id),
            payload={"batchId": str(batch_id), "uploadedBy": str(keep["created_by_actor_id"]),
                     "uploadedByRole": "PC"},
            correlation_id=correlation,
        )
        details = {"documentType": unit["document_type_key"], "pages": pages, "fileName": filename,
                   "from": copy["journey_reference"], "to": keep["journey_reference"], "by": "Verigence",
                   "why": "Duplicate booking merged: the document only the copy held", "job": tag}
        record_activity(
            connection, tenant_id=tenant_id, journey_id=keep["journey_id"], event_type="DOCUMENT_COPIED_FROM_DUPLICATE",
            subject_type="UPLOAD_BATCH", subject_id=str(batch_id), details=details, correlation_id=correlation,
        )
        record_activity(
            connection, tenant_id=tenant_id, journey_id=copy["journey_id"], event_type="DOCUMENT_COPIED_TO_KEPT_BOOKING",
            subject_type="DOCUMENT_PAGE", subject_id=str(unit["queue_id"]), details=details, correlation_id=correlation,
        )
    return batch_id


def run_duplicate_merge(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    tenant_id, commit = parse_tag(tag)
    now = datetime.now(UTC)
    items: list[dict[str, Any]] = []
    totals = {"pairs": 0, "would_copy": 0, "copied": 0, "skipped": 0, "failed": 0}
    for entry in reports or []:
        totals["pairs"] += 1
        try:
            keep_ref, copy_ref, kinds = parse_entry(entry)
        except ValueError as exc:
            items.append({"entry": entry, "outcome": "FAILED", "why": str(exc)})
            totals["failed"] += 1
            continue
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            keep, copy = _journey(connection, tenant_id, keep_ref), _journey(connection, tenant_id, copy_ref)
            problem = None
            if keep is None or copy is None:
                problem = f"no booking {keep_ref if keep is None else copy_ref}"
            elif keep["journey_id"] == copy["journey_id"]:
                problem = "the two references are one booking"
            elif keep["closed"] or copy["closed"] or keep["review_completed_at_utc"] or copy["review_completed_at_utc"]:
                problem = "a booking of the pair is cancelled, a duplicate or review-complete"
            plan: list[tuple[str, dict[str, Any] | None, str | None]] = []
            if problem is None:
                for kind in kinds:
                    if _holds(connection, tenant_id, keep["journey_id"], kind):
                        plan.append((kind, None, "the kept booking already holds one"))
                        continue
                    units = _units(connection, tenant_id, copy["journey_id"], kind)
                    if not units:
                        plan.append((kind, None, "the copy holds none read from a stored file"))
                    plan.extend((kind, unit, None) for unit in units)
        if problem is not None:
            items.append({"entry": entry, "outcome": "SKIPPED", "why": problem})
            totals["skipped"] += 1
            continue
        for kind, unit, why in plan:
            item: dict[str, Any] = {"keep": keep_ref, "copy": copy_ref, "documentType": kind}
            if unit is None:
                item.update(outcome="SKIPPED", why=why)
                totals["skipped"] += 1
            else:
                item.update(queueId=str(unit["queue_id"]), unitKind=unit["unit_kind"],
                            pages=list(unit["page_numbers"] or [unit["page_number"]]),
                            file=unit["original_filename"])
                if not commit:
                    item["outcome"] = "WOULD_COPY"
                    totals["would_copy"] += 1
                else:
                    try:
                        item["newBatchId"] = str(_copy_unit(engine, tenant_id, keep, copy, unit, tag=tag, now=now))
                        item["outcome"] = "COPIED"
                        totals["copied"] += 1
                    except Exception as exc:  # one document never stops the others
                        item.update(outcome="FAILED", why=f"{type(exc).__name__}: {exc}"[:300])
                        totals["failed"] += 1
                        logger.warning("duplicate_merge_item_failed", tag=tag, entry=entry, exc_info=True)
            items.append(item)
            logger.info("duplicate_merge_item", tag=tag, tenant_id=tenant_id, commit=commit, **item)
    logger.info("duplicate_merge_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
    return {"commit": commit, **totals, "items": items}
