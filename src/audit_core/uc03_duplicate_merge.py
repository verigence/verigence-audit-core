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


class NotHeldAtDi(RuntimeError):
    """Document Intelligence no longer holds the unit's reading: there is nothing to move."""


def _di_context_refs(engine: Engine, tenant_id: str, journey_ids: list[UUID]) -> tuple[str, list[str]]:
    """A service token and each Journey's DI storage context (established at DI when new)."""
    from audit_core.uc03_document_capture_v2 import (
        _ensure_di_context,
        get_di_client,
        get_security_oauth_client,
    )

    security_client, di_client = get_security_oauth_client(), get_di_client()
    refs: list[str] = []
    token = ""
    for journey_id in journey_ids:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            ref, token = _ensure_di_context(connection=connection, engine=engine, tenant_id=tenant_id,
                                            journey_id=journey_id, security_client=security_client,
                                            di_client=di_client)
        refs.append(ref)
    return token, refs


def _move_unit(engine: Engine, tenant_id: str, keep: dict[str, Any], copy: dict[str, Any], unit: dict[str, Any],
               *, tag: str, now: datetime) -> UUID:
    """Carry one unit the copy already read without reading it again (product owner
    2026-11-24: "no double reading"). Document Intelligence moves the document -- its
    classification and reading -- to the kept booking's context; the kept booking gets the
    stored file as an upload of its own with the unit already submitted, and its reconcile
    copies the values DI holds (no model call). Same upload id as _copy_unit, so either is
    idempotent against the other. NotHeldAtDi when DI no longer holds the reading."""
    from audit_core.di_capture_v2_client import DiCaptureV2Error
    from audit_core.uc03_document_capture_v2 import get_di_capture_v2_client
    from audit_core.uc03_p2_object_keys import original_key, upload_labels
    from audit_core.uc03_p2_runtime import enqueue_work, record_activity
    from audit_core.uc03_p2_storage import get_p2_document_storage

    batch_id = new_batch_id(unit["queue_id"], keep["journey_id"])
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        if connection.execute(text("SELECT 1 FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND batch_id=:b"),
                              {"t": tenant_id, "b": batch_id}).scalar_one_or_none():
            return batch_id  # carried already
        source = connection.execute(
            text(
                """
                SELECT q.queue_id, q.unit_kind, q.page_number, q.page_numbers, q.page_sha256, q.page_object_key,
                       q.di_document_id, q.classified_document_type, q.template_key, q.business_stage,
                       q.di_state, q.di_processing_status, q.group_source, q.display_name, q.quality_confirmed,
                       d.stage_code, d.classified_document_type_key, d.capture_status, d.classified_at_utc
                FROM auditcore.p2_document_queue q
                LEFT JOIN auditcore.document_capture_v2_documents d
                  ON d.tenant_id=q.tenant_id AND d.journey_id=q.journey_id AND d.di_document_id=q.di_document_id
                WHERE q.tenant_id=:t AND q.queue_id=:q
                """
            ),
            {"t": tenant_id, "q": unit["queue_id"]},
        ).mappings().one()
        members = connection.execute(
            text("SELECT page_number, page_sha256 FROM auditcore.p2_document_queue WHERE tenant_id=:t "
                 "AND merged_into_queue_id=:q AND unit_kind='PAGE' ORDER BY page_number, created_at_utc, queue_id"),
            {"t": tenant_id, "q": unit["queue_id"]},
        ).mappings().all() if source["unit_kind"] == "GROUP" else []
    document_id = str(source["di_document_id"])

    token, (copy_ref, keep_ref) = _di_context_refs(engine, tenant_id, [copy["journey_id"], keep["journey_id"]])
    try:
        get_di_capture_v2_client().move_document(
            token=token, tenant_id=tenant_id, external_context_ref=copy_ref, document_id=document_id,
            target_external_context_ref=keep_ref)
    except DiCaptureV2Error as exc:
        if exc.status_code == 404:
            raise NotHeldAtDi(document_id) from exc
        raise

    storage = get_p2_document_storage()
    payload = storage.get_object(str(source["page_object_key"]))
    if not payload:
        raise RuntimeError(f"stored file {source['page_object_key']} is empty")
    pages = [int(p) for p in (source["page_numbers"] or [source["page_number"]])]
    is_pdf = str(source["page_object_key"]).endswith(".pdf") or payload[:5] == b"%PDF-"
    stem = str(unit.get("original_filename") or "document").rsplit(".", 1)[0]
    kind = unit.get("document_type_key") or source["classified_document_type"] or "document"
    filename = (f"{kind} from {copy['journey_reference']} - {stem} "
                f"page{'s' if len(pages) > 1 else ''} {'-'.join(map(str, pages))}" + (".pdf" if is_pdf else ""))
    content_type = "application/pdf" if is_pdf else str(unit.get("content_type") or "application/octet-stream")
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        labels = upload_labels(connection, tenant_id=tenant_id, journey_id=keep["journey_id"])
    key = original_key(tenant_id, keep["journey_id"], batch_id, filename=filename, outlet_name=labels[0],
                       pc_name=labels[1], customer_name=labels[2], now=now)
    storage.put_object(key, payload, content_type=content_type)  # outside any transaction; same key on a retry
    correlation = f"p2-duplicate-merge:{unit['queue_id']}"
    uploader = str(keep["created_by_actor_id"])
    numbers = list(range(1, len(pages) + 1))
    client_upload_id = f"carried-{unit['queue_id']}"
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        inserted = connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, client_upload_id, original_filename, content_type,
                    size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id, correlation_id
                ) VALUES (:t, :b, :j, :client, :name, :ct, :size, :pages, :key, 'PROCESSING', :by, :correlation)
                ON CONFLICT DO NOTHING
                RETURNING batch_id
                """
            ),
            {"t": tenant_id, "b": batch_id, "j": keep["journey_id"], "client": f"dup-merge-{unit['queue_id']}",
             "name": filename[:250], "ct": content_type, "size": len(payload), "pages": len(pages), "key": key,
             "by": uploader, "correlation": correlation},
        ).scalar_one_or_none()
        if inserted is None:
            return batch_id
        # The unit as it was read: already submitted to DI, which now lists it under the kept
        # booking; the reconcile copies its values and settles it (no reading).
        queue_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_document_queue (
                    tenant_id, batch_id, journey_id, page_number, page_numbers, page_sha256, page_object_key,
                    client_upload_id, di_document_id, classified_document_type, template_key, business_stage,
                    queue_status, di_state, di_processing_status, di_submitted_at_utc, unit_kind, group_source,
                    display_name, quality_confirmed, correlation_id
                ) VALUES (
                    :t, :b, :j, 1, :numbers, :sha, :key, :client, :d, :type, :template, :stage,
                    'CLASSIFYING', :di_state, :di_processing, now(), :kind, :group_source,
                    :display, :quality, :correlation
                ) RETURNING queue_id
                """
            ),
            {"t": tenant_id, "b": batch_id, "j": keep["journey_id"], "numbers": numbers,
             "sha": source["page_sha256"], "key": key, "client": client_upload_id, "d": source["di_document_id"],
             "type": source["classified_document_type"], "template": source["template_key"],
             "stage": source["business_stage"], "di_state": source["di_state"],
             "di_processing": source["di_processing_status"], "kind": source["unit_kind"],
             "group_source": source["group_source"] if source["unit_kind"] == "GROUP" else None,
             "display": source["display_name"], "quality": bool(source["quality_confirmed"]),
             "correlation": correlation},
        ).scalar_one()
        # A document's own pages keep their fingerprints, so a later file repeating them is
        # recognised as already on this booking (uc03_p2_same_file.carry_plan).
        for number, member in enumerate(members, start=1):
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_document_queue (
                        tenant_id, batch_id, journey_id, page_number, page_numbers, page_sha256, page_object_key,
                        client_upload_id, queue_status, unit_kind, merged_into_queue_id, retired_at_utc,
                        correlation_id
                    ) VALUES (:t, :b, :j, :n, ARRAY[:n], :sha, :key, :client, 'MERGED', 'PAGE', :into, now(),
                              :correlation)
                    """
                ),
                {"t": tenant_id, "b": batch_id, "j": keep["journey_id"], "n": number, "sha": member["page_sha256"],
                 "key": key, "client": f"{client_upload_id}-p{number}", "into": queue_id,
                 "correlation": correlation},
            )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_capture_v2_documents (
                    tenant_id, journey_id, stage_code, di_document_id, client_upload_id,
                    classified_document_type_key, capture_status, original_filename, content_type,
                    created_by_actor_id, classified_at_utc
                ) VALUES (:t, :j, :stage, :d, :client, :type, :status, :name, :ct, :by, :classified)
                ON CONFLICT (tenant_id, journey_id, di_document_id) DO NOTHING
                """
            ),
            {"t": tenant_id, "j": keep["journey_id"], "stage": source["stage_code"] or "BOOKING",
             "d": source["di_document_id"], "client": client_upload_id,
             "type": source["classified_document_type_key"], "status": source["capture_status"] or "CLASSIFIED",
             "name": filename[:500], "ct": content_type, "by": uploader, "classified": source["classified_at_utc"]},
        )
        enqueue_work(
            connection, tenant_id=tenant_id, journey_id=keep["journey_id"], work_type="JOURNEY_RECONCILE",
            work_key=str(keep["journey_id"]), payload={"uploadedBy": uploader, "uploadedByRole": "PC"},
            correlation_id=correlation,
        )
        details = {"documentType": kind, "pages": pages, "fileName": filename,
                   "from": copy["journey_reference"], "to": keep["journey_reference"], "by": "Verigence",
                   "why": "Duplicate booking merged: the document only the copy held (moved with its reading)",
                   "job": tag}
        record_activity(
            connection, tenant_id=tenant_id, journey_id=keep["journey_id"], event_type="DOCUMENT_COPIED_FROM_DUPLICATE",
            subject_type="UPLOAD_BATCH", subject_id=str(batch_id), details=details, correlation_id=correlation,
        )
        record_activity(
            connection, tenant_id=tenant_id, journey_id=copy["journey_id"], event_type="DOCUMENT_COPIED_TO_KEPT_BOOKING",
            subject_type="DOCUMENT_PAGE", subject_id=str(unit["queue_id"]), details=details, correlation_id=correlation,
        )
    logger.info("p2_carried_unit_moved", tenant_id=tenant_id, di_document_id=document_id,
                from_journey=str(copy["journey_id"]), to_journey=str(keep["journey_id"]), batch_id=str(batch_id))
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
