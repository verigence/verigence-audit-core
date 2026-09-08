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
    materialize_reviewed_delivery_business_values,
)

logger = logging.getLogger(__name__)

_MACHINE_ACTOR = "SYSTEM:DI_AUTO"


def _delivery_documents_from_durable_store(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[Any]:
    rows = connection.execute(
        text(
            """
            SELECT di_document_id, evidence_id, source_document_type_key,
                   field_key, effective_value, confidence_score
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='DELIVERY'
            ORDER BY di_document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
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
        documents = _delivery_documents_from_durable_store(
            connection, tenant_id=tenant_id, journey_id=journey_id
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


__all__ = ["materialize_delivery_documents_from_durable_store"]
