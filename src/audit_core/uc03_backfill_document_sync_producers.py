"""uc03_backfill_document_sync_producers.py — retroactively run the newer
durable-state-driven UC03 producers against every existing Journey.

Every producer wired into the per-document sync trigger only ever runs when
a *new* document sync happens (a fresh DI webhook callback, PC Review
Confirm, or a manual /resync call on one Journey). A Journey whose documents
were all confirmed *before* a given producer was added never gets it run at
all -- not because the producer is broken, but because nothing ever
retriggered the sync pipeline for it. Confirmed directly: no flags/updated
fields appearing on old, already-processed deliveries after shipping the
delivery-date, SKU-invoice-fallback, customer/dealer-identity, and
duplicate-receipt producers.

This runs the same four producers directly against durable storage --
none of them need a fresh DI call, all four are idempotent/self-healing by
design, so this is safe to run repeatedly and cheap (no DI traffic at all):

  1. materialize_delivery_documents_from_durable_store -- delivery date
     (and every other Delivery materializer: insurance, vehicle,
     registration, finance, invoices, receipts) re-derived from whatever is
     already durably stored.
  2. sync_model_resolution_from_invoice -- SKU fallback from a Delivery
     invoice, for a Booking that never resolved one.
  3. sync_customer_identity_consistency -- customer-name AND receipt
     dealer-name checks (WRONG_DOCUMENT).
  4. sync_duplicate_receipt_detection -- DUPLICATE_RECEIPT.

Each Journey runs in its own transaction so one Journey's failure can't
roll back progress on the rest; failures are collected, not raised.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.uc03_customer_identity_consistency import (
    sync_customer_identity_consistency,
)
from audit_core.uc03_delivery_post_extraction_materialization import (
    materialize_delivery_documents_from_durable_store,
)
from audit_core.uc03_duplicate_receipt_detection import sync_duplicate_receipt_detection
from audit_core.uc03_model_resolution import sync_model_resolution_from_invoice

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/admin/backfill",
    tags=["uc03-backfill"],
)

_CORRELATION_ID = "backfill:document-sync-producers"


def _all_journey_ids_with_extracted_fields(connection, *, tenant_id: str) -> list[UUID]:
    return list(
        connection.execute(
            text(
                """
                SELECT DISTINCT journey_id
                FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id = :tenant_id
                """
            ),
            {"tenant_id": tenant_id},
        ).scalars().all()
    )


def backfill_document_sync_producers_for_tenant(
    engine: Engine, *, tenant_id: str
) -> dict[str, Any]:
    """Run all four producers against every Journey with any confirmed
    document data. Never raises; a per-Journey failure is recorded and
    skipped, not fatal to the rest of the run."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journey_ids = _all_journey_ids_with_extracted_fields(connection, tenant_id=tenant_id)

    processed = 0
    failed: list[str] = []
    totals: dict[str, int] = {
        "deliveryMaterializations": 0,
        "skuResolutionsFromInvoice": 0,
        "identityFindingsRaised": 0,
        "identityFindingsResolved": 0,
        "duplicateReceiptFindingsRaised": 0,
        "duplicateReceiptFindingsResolved": 0,
    }

    for journey_id in journey_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)

                delivery_result = materialize_delivery_documents_from_durable_store(
                    connection, tenant_id=tenant_id, journey_id=journey_id
                )
                if delivery_result.get("deliveryDateSet"):
                    totals["deliveryMaterializations"] += 1

                sku_result = sync_model_resolution_from_invoice(
                    connection, tenant_id=tenant_id, journey_id=journey_id,
                    correlation_id=_CORRELATION_ID,
                )
                if sku_result.get("resolved"):
                    totals["skuResolutionsFromInvoice"] += 1

                identity_result = sync_customer_identity_consistency(
                    connection, tenant_id=tenant_id, journey_id=journey_id,
                    correlation_id=_CORRELATION_ID,
                )
                totals["identityFindingsRaised"] += identity_result.get("raised", 0)
                totals["identityFindingsResolved"] += identity_result.get("resolved", 0)

                duplicate_result = sync_duplicate_receipt_detection(
                    connection, tenant_id=tenant_id, journey_id=journey_id,
                    correlation_id=_CORRELATION_ID,
                )
                totals["duplicateReceiptFindingsRaised"] += duplicate_result.get("raised", 0)
                totals["duplicateReceiptFindingsResolved"] += duplicate_result.get("resolved", 0)

            processed += 1
        except Exception:
            logger.warning(
                "uc03_backfill_journey_failed",
                extra={"tenant_id": tenant_id, "journey_id": str(journey_id)},
                exc_info=True,
            )
            failed.append(str(journey_id))

    return {
        "tenantId": tenant_id,
        "journeysConsidered": len(journey_ids),
        "journeysProcessed": processed,
        "journeysFailed": failed,
        **totals,
    }


class BackfillResult(BaseModel):
    tenantId: str
    journeysConsidered: int
    journeysProcessed: int
    journeysFailed: list[str]
    deliveryMaterializations: int
    skuResolutionsFromInvoice: int
    identityFindingsRaised: int
    identityFindingsResolved: int
    duplicateReceiptFindingsRaised: int
    duplicateReceiptFindingsResolved: int


@router.post("/document-sync-producers", response_model=BackfillResult)
def backfill_document_sync_producers(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> BackfillResult:
    del admin_request
    result = backfill_document_sync_producers_for_tenant(engine, tenant_id=tenant_id)
    return BackfillResult.model_validate(result)


__all__ = ["backfill_document_sync_producers_for_tenant", "router"]
