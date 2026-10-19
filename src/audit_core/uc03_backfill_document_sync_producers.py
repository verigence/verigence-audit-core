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

from fastapi import APIRouter, Depends, Query
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


_SKU_CORRELATION_ID = "backfill:sku-resolution"


def _sku_counts(engine: Engine, *, tenant_id: str) -> tuple[int, int]:
    """(journeys with a pinned SKU, journeys without one) among Journeys that have a product row."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        row = connection.execute(
            text(
                """
                SELECT count(DISTINCT journey_id) FILTER (WHERE product_sku_id IS NOT NULL),
                       count(DISTINCT journey_id) FILTER (WHERE product_sku_id IS NULL)
                FROM auditcore.journey_products
                WHERE tenant_id = :tenant_id
                """
            ),
            {"tenant_id": tenant_id},
        ).one()
    return int(row[0] or 0), int(row[1] or 0)


def backfill_sku_resolution_for_tenant(
    engine: Engine, *, tenant_id: str, offset: int = 0, limit: int | None = None
) -> dict[str, Any]:
    """Run SKU resolution (booking model/price match, then the Delivery-invoice fallback) for
    the Project's Journeys, in a stable order, ``limit`` at a time starting at ``offset`` (all of
    them when ``limit`` is None). These are the same idempotent producers the app runs when a
    document is confirmed or "Run All Rules" is pressed; each Journey is its own transaction and
    a failure is listed, never fatal to the rest. Callers loop on ``nextOffset`` so no single
    request runs long enough for the edge proxy to time out."""
    from audit_core.uc03_async_sync_tasks import (
        sync_model_resolution_from_invoice_with_escalation,
        sync_model_resolution_with_escalation,
    )

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys_total = int(
            connection.execute(
                text("SELECT count(*) FROM auditcore.journeys WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            ).scalar_one()
        )
        journey_ids = list(
            connection.execute(
                text(
                    """
                    SELECT journey_id FROM auditcore.journeys WHERE tenant_id = :tenant_id
                    ORDER BY created_at_utc, journey_id
                    OFFSET :offset LIMIT :limit
                    """
                ),
                {"tenant_id": tenant_id, "offset": offset, "limit": limit},
            ).scalars().all()
        )
    next_offset = offset + len(journey_ids)
    with_sku_before, without_sku_before = _sku_counts(engine, tenant_id=tenant_id)

    processed = 0
    failed: list[str] = []
    errors = 0
    for journey_id in journey_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                for resolve in (
                    sync_model_resolution_with_escalation,
                    sync_model_resolution_from_invoice_with_escalation,
                ):
                    result = resolve(
                        connection, tenant_id=tenant_id, journey_id=journey_id,
                        correlation_id=_SKU_CORRELATION_ID,
                    )
                    if result.get("error"):
                        errors += 1
            processed += 1
        except Exception:
            logger.warning(
                "uc03_sku_backfill_journey_failed",
                extra={"tenant_id": tenant_id, "journey_id": str(journey_id)},
                exc_info=True,
            )
            failed.append(str(journey_id))

    with_sku_after, without_sku_after = _sku_counts(engine, tenant_id=tenant_id)
    return {
        "tenantId": tenant_id,
        "journeysTotal": journeys_total,
        "journeysConsidered": len(journey_ids),
        "journeysProcessed": processed,
        "journeysFailed": failed,
        "nextOffset": next_offset if next_offset < journeys_total else None,
        "withSkuBefore": with_sku_before,
        "withSkuAfter": with_sku_after,
        "withoutSkuBefore": without_sku_before,
        "withoutSkuAfter": without_sku_after,
        "stepErrors": errors,
    }


class SkuResolutionBackfillResult(BaseModel):
    tenantId: str
    journeysTotal: int
    nextOffset: int | None
    journeysConsidered: int
    journeysProcessed: int
    journeysFailed: list[str]
    withSkuBefore: int
    withSkuAfter: int
    withoutSkuBefore: int
    withoutSkuAfter: int
    stepErrors: int


@router.post("/sku-resolution", response_model=SkuResolutionBackfillResult)
def backfill_sku_resolution(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=25)] = 3,
) -> SkuResolutionBackfillResult:
    """One batch of Journeys; repeat with ``nextOffset`` until it is null."""
    del admin_request
    return SkuResolutionBackfillResult.model_validate(
        backfill_sku_resolution_for_tenant(engine, tenant_id=tenant_id, offset=offset, limit=limit)
    )


__all__ = ["backfill_document_sync_producers_for_tenant", "backfill_sku_resolution_for_tenant", "router"]
