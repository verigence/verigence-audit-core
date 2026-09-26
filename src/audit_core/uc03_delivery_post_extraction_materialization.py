"""uc03_delivery_post_extraction_materialization.py — Delivery's per-document,
durable-state-based counterpart to ``uc03_post_extraction_materialization.py``.

Booking's per-document materializer (``materialize_machine_booking_values``)
re-derives every semantic field's current winner from
``journey_document_extracted_fields`` on every call -- it never operates on
"just this one document", so calling it after every single document confirms
is safe: whichever document last landed, the read always reflects everything
currently known.

Delivery's existing materializer (``materialize_reviewed_delivery_business_
values``, used by the PC confirm flow) is shaped differently: it takes a
``documents`` list and resolves cross-document source priority (which
invoice wins for the vehicle VIN, say -- see ``_VEHICLE_SOURCE_PRIORITY`` in
``uc03_delivery_review_materialization.py``) *from that list*, not from
durable state. Feeding it one newly-confirmed document at a time would
silently drop that priority logic whenever a lower-priority document
happened to confirm after a higher-priority one already had.

So this module keeps Delivery's materializer itself untouched and instead
rebuilds its input the way Booking's resolver already works: read every
currently-confirmed Delivery document back out of
``journey_document_extracted_fields`` (durable, already includes every
document ever confirmed -- not just the one that triggered this pass) and
feed the *whole* reconstructed set in, every time. Delivery has many more
document types than Booking (invoices, insurance, RTO, receipts, bank
statements...), which is exactly why this matters more here, not less --
each document type's own confirmation is a fresh chance to re-resolve
priority across everything currently known.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_delivery_review_materialization import (
    materialize_delivery_insurance,
    materialize_delivery_registration,
    materialize_reviewed_delivery_business_values,
)

logger = logging.getLogger(__name__)

_MACHINE_ACTOR = "SYSTEM:DI_AUTO"


def _documents_from_durable_store(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage_code: str
) -> list[Any]:
    # Joined to evidence and filtered to association_status='ACTIVE' (added
    # 2026-09-26): _sync_booking_document -- the ONLY writer of this table --
    # already refuses to write a single row here unless the document's own
    # evidence is ACTIVE at that moment (`if link is None or association_
    # status != "ACTIVE": return 0`), so every row already in this table
    # today was written while its document's evidence was ACTIVE -- this
    # join drops nothing that isn't voided. What it does fix: once a
    # document is deleted (delete_unified_document voids its evidence row
    # rather than deleting these facts outright -- deletion is revoked for
    # this table at the database level), that document's already-durable
    # facts must stop feeding canonical materialization on every later
    # re-run, without ever touching (or being able to touch) the permanent
    # facts themselves.
    #
    # Joined on di_document_id, NOT this table's own evidence_id column:
    # migration 0051 explicitly dropped evidence_id's NOT NULL ("current V2
    # rows may instead use DI document + canonical field + fact version
    # identity, so the two legacy identifiers become nullable") -- a plain
    # V2 row's own evidence_id is commonly NULL. di_document_id, on both
    # this table and evidence, has been NOT NULL since its original
    # migration (0031) and was never altered; evidence also has a UNIQUE
    # (tenant_id, di_document_id) -- the exact same pair _sync_booking_
    # document's own evidence lookup already keys on. Joining on evidence_id
    # instead would have silently dropped every current V2 document's facts
    # from materialization, not just voided ones -- caught before shipping.
    rows = connection.execute(
        text(
            """
            SELECT f.di_document_id, f.evidence_id, f.source_document_type_key,
                   f.field_key, f.effective_value, f.confidence_score
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e
              ON e.tenant_id=f.tenant_id AND e.di_document_id=f.di_document_id
            WHERE f.tenant_id=:tenant_id AND f.journey_id=:journey_id
              AND f.stage_code=:stage_code
              AND e.association_status='ACTIVE'
            ORDER BY f.di_document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).mappings().all()

    grouped: dict[UUID, dict[str, Any]] = {}
    for row in rows:
        document_id = row["di_document_id"]
        item = grouped.setdefault(
            document_id,
            {
                "documentId": document_id,
                "evidenceId": row["evidence_id"],
                "documentTypeKey": row["source_document_type_key"],
                # Every row here already passed DI confirmation to be
                # durably captured in the first place.
                "extractionState": "READY",
                "fields": [],
            },
        )
        value = row["effective_value"]
        if value is None:
            continue
        item["fields"].append(
            SimpleNamespace(
                fieldKey=row["field_key"],
                value=value,
                confidenceScore=(
                    float(row["confidence_score"])
                    if row["confidence_score"] is not None
                    else None
                ),
            )
        )
    return [SimpleNamespace(**item) for item in grouped.values()]


def materialize_delivery_documents_from_durable_store(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any]:
    """Re-derive Delivery's canonical tables from every currently-confirmed
    Delivery document -- the same projection PC confirm runs, just triggered
    async per document instead of waiting for confirm. Always rebuilds the
    full document set from durable storage (never a single document), so
    cross-document source-priority resolution stays correct regardless of
    which document's confirmation triggered this pass. Idempotent (every
    typed writer underneath upserts); never raises.
    """
    try:
        documents = _documents_from_durable_store(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="DELIVERY"
        )
        if not documents:
            return {"skipped": True, "reason": "no_documents"}
        result = materialize_reviewed_delivery_business_values(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            documents=documents,
            actor_id=_MACHINE_ACTOR,
        )
        logger.info(
            "uc03_delivery_post_extraction_materialized",
            extra={
                "tenant_id": tenant_id,
                "journey_id": str(journey_id),
                "document_count": len(documents),
                **result,
            },
        )
        return result
    except Exception:
        logger.warning(
            "materialize_delivery_documents_from_durable_store failed", exc_info=True
        )
        return {"error": True}


def materialize_booking_documents_from_durable_store(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any]:
    """Fill canonical facts from every currently-confirmed Booking document,
    for every document-type-scoped materializer that is genuinely stage-
    agnostic (filters candidates by document TYPE, writes a table keyed
    (tenant_id, journey_id) with no stage column at all).

    Originally written narrowly for insurance only (materialize_delivery_
    insurance): confirmed live, an Insurance Cover document uploaded and
    confirmed during Booking showed its extracted fields (insurer name,
    chassis number, ...) in the raw reviewed-fields viewer, but never
    reached auditcore.insurance_records no matter how many times Resync
    ran, because the one code path that calls materialize_delivery_
    insurance never fired for stage_code == "BOOKING" at all -- not a
    confirmation-status gate, not a stale row, a genuinely missing call.
    That fix was never generalized to the other document-type-scoped
    materializers with the exact same shape (e.g. materialize_delivery_
    registration for an RTO Challan uploaded at Booking) -- leaving the
    identical bug open for every one of them except insurance. Fixed here
    by calling every stage-agnostic materializer, not just one.

    Deliberately still not the full Delivery bundle (materialize_reviewed_
    delivery_business_values) -- vehicle/finance/scrappage/invoices/
    receipts/bank-statement reconciliation carry real Delivery-only side
    effects (disbursement resolution, payment reconciliation) that
    genuinely don't apply pre-Delivery, unlike a plain per-document-type
    field fill.
    """
    try:
        documents = _documents_from_durable_store(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code="BOOKING"
        )
        if not documents:
            return {"skipped": True, "reason": "no_documents"}
        insurance_written = materialize_delivery_insurance(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            documents=documents,
        )
        registration_written = materialize_delivery_registration(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            documents=documents,
        )
        result = {
            "insuranceFieldsWritten": insurance_written,
            "registrationFieldsWritten": registration_written,
        }
        logger.info(
            "uc03_booking_documents_materialized",
            extra={
                "tenant_id": tenant_id,
                "journey_id": str(journey_id),
                "document_count": len(documents),
                **result,
            },
        )
        return result
    except Exception:
        logger.warning(
            "materialize_booking_documents_from_durable_store failed", exc_info=True
        )
        return {"error": True}


__all__ = [
    "materialize_booking_documents_from_durable_store",
    "materialize_delivery_documents_from_durable_store",
]
