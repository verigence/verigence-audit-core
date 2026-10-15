"""Machine-facing Booking helpers, for a registered ServiceIntegration.

Two things the Booking flow already knows how to do, offered to another system
that holds the booking itself. A dealership application reads a Booking Form
and its payment receipts through Document Intelligence, and then needs the two
answers Audit Core is the authority on:

  which SKU the vehicle text names, against the OEM price masters of that
  tenant on that date; and

  whether the receipts collected so far reach the tenant's minimum booking
  amount, counting duplicates once.

Both are computed by the very functions the journey flow uses -- ``resolve_
model_text`` runs the same ladder as ``sync_model_resolution``, and the
minimum-payment answer here applies ``compute_duplicate_groups`` and the same
chronological running total as ``evaluate_minimum_booking_payment``. One
definition of each rule, never a second that drifts.

Both are read-only and stateless. Nothing is pinned, no finding is raised, no
Task is spawned and no journey is required or created: the caller owns the
booking, and these answer a question about it rather than taking it over. That
is also why neither needs a human principal -- there is no journey to scope to
and nothing is changed, so a ServiceIntegration token is the whole of the
authority required, exactly as for DI's own document-link callback.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection
from audit_core.errors import AuditCoreError
from audit_core.security import ServiceIntegrationPrincipal
from audit_core.uc03_duplicate_receipt_detection import (
    ReceiptRecord,
    compute_duplicate_groups,
    normalize_receipt_date,
    normalize_receipt_number,
)
from audit_core.uc03_model_resolution import resolve_model_text
from audit_core.uc03_pc_booking_documents import require_audit_service_principal

router = APIRouter(prefix="/v1/internal/booking", tags=["uc03-integration"])

# Receipts arrive from the caller rather than from this service's own tables,
# so they are labelled with the stage and type the Booking rules group on.
_BOOKING_STAGE = "BOOKING"
_RECEIPT_TYPE = "payment_receipt"


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class SkuResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenantId: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=200)
    variant: str | None = Field(default=None, max_length=240)
    colour: str | None = Field(default=None, max_length=200)
    # The business date the price masters are read on. A booking taken last
    # month is resolved against the list that stood last month, not today's.
    on: date | None = None
    totalPrice: Decimal | None = None
    exShowroomPrice: Decimal | None = None
    registrationBasis: str = Field(default="INDIVIDUAL", max_length=20)


class SkuCandidateOut(BaseModel):
    productSkuId: UUID
    skuCode: str
    modelName: str
    variantName: str | None = None
    colourName: str | None = None
    exShowroomPrice: Decimal | None = None
    totalPrice: Decimal | None = None


class SkuResolveResponse(BaseModel):
    # RESOLVED one SKU; AMBIGUOUS several, listed; NONE nothing in the masters;
    # UNAVAILABLE no price list stands for this tenant on this date.
    outcome: str
    matchStage: str | None = None
    reason: str | None = None
    sku: SkuCandidateOut | None = None
    candidates: list[SkuCandidateOut] = Field(default_factory=list)


class PaymentReceiptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The caller's own identifier for the receipt, echoed back so it can tell
    # which of the ones it sent were counted and which were set aside.
    reference: str = Field(min_length=1, max_length=120)
    amount: Decimal
    receiptDate: str | None = Field(default=None, max_length=40)
    receiptNumber: str | None = Field(default=None, max_length=120)


class MinimumPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenantId: str = Field(min_length=1, max_length=128)
    receipts: list[PaymentReceiptIn] = Field(min_length=1, max_length=100)
    # Left out, the tenant's own configured minimum is used.
    minimumAmount: Decimal | None = None


class MinimumPaymentResponse(BaseModel):
    met: bool
    minimumAmount: Decimal
    totalCounted: Decimal
    # The receipt that took the running total over the line, so the caller can
    # date the confirmation the way the Booking flow dates it.
    confirmingReference: str | None = None
    confirmingDate: str | None = None
    countedReferences: list[str] = Field(default_factory=list)
    duplicateReferences: list[str] = Field(default_factory=list)


@router.post("/sku-resolution", response_model=SkuResolveResponse)
def resolve_sku(
    payload: SkuResolveRequest,
    service_principal: Annotated[ServiceIntegrationPrincipal, Depends(require_audit_service_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> SkuResolveResponse:
    set_tenant_context(connection, payload.tenantId)
    result = resolve_model_text(
        connection,
        tenant_id=payload.tenantId,
        effective_on=payload.on or date.today(),
        model_name=payload.model,
        variant_name=payload.variant,
        colour_name=payload.colour,
        offered_total=payload.totalPrice,
        offered_ex_showroom=payload.exShowroomPrice,
        registration_basis_code=payload.registrationBasis,
    )
    if result.get("skipped"):
        return SkuResolveResponse(outcome="UNAVAILABLE", reason=str(result.get("reason") or "unavailable"))

    matched = list(result.get("matched") or [])
    stage = str(result.get("matchStage") or "NONE")
    candidates = [
        SkuCandidateOut(
            productSkuId=row["product_sku_id"],
            skuCode=str(row["sku_code"]),
            modelName=str(row["model_name"]),
            variantName=row.get("variant_name"),
            colourName=row.get("colour_name"),
            exShowroomPrice=row.get("master_ex_showroom"),
            totalPrice=row.get("master_total_individual"),
        )
        # A long shortlist is of no use to anybody; the Booking flow caps its
        # own candidate payload at ten for the same reason.
        for row in matched[:10]
    ]
    if len(matched) == 1:
        return SkuResolveResponse(outcome="RESOLVED", matchStage=stage, sku=candidates[0], candidates=candidates)
    if not matched:
        return SkuResolveResponse(outcome="NONE", matchStage=stage)
    return SkuResolveResponse(outcome="AMBIGUOUS", matchStage=stage, candidates=candidates)


@router.post("/minimum-payment", response_model=MinimumPaymentResponse)
def check_minimum_payment(
    payload: MinimumPaymentRequest,
    service_principal: Annotated[ServiceIntegrationPrincipal, Depends(require_audit_service_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> MinimumPaymentResponse:
    set_tenant_context(connection, payload.tenantId)
    minimum = payload.minimumAmount if payload.minimumAmount is not None else _tenant_minimum(connection, payload.tenantId)

    # The caller's references are strings; duplicate grouping keys on a UUID,
    # so each receipt is given one for the length of this call only.
    ids: dict[UUID, PaymentReceiptIn] = {}
    records: list[ReceiptRecord] = []
    for receipt in payload.receipts:
        document_id = uuid4()
        ids[document_id] = receipt
        records.append(
            ReceiptRecord(
                document_id=document_id,
                stage_code=_BOOKING_STAGE,
                document_type_key=_RECEIPT_TYPE,
                receipt_number=normalize_receipt_number(receipt.receiptNumber),
                amount=_decimal(receipt.amount),
                receipt_date=normalize_receipt_date(receipt.receiptDate),
            )
        )

    # The same grouping the Booking flow excludes by: within a group the
    # earliest-dated receipt counts and the rest are set aside, so the same
    # money reaching us twice is not counted twice.
    excluded: set[UUID] = set()
    for group in compute_duplicate_groups(records):
        for document in group.documents[1:]:
            excluded.add(document.document_id)

    # Chronological, because the rule confirms the booking on the receipt that
    # takes the total over the line, and that date is what the caller records.
    ordered = sorted(records, key=lambda r: (r.receipt_date or "", str(r.document_id)))
    total = Decimal(0)
    confirming: ReceiptRecord | None = None
    counted: list[str] = []
    for record in ordered:
        if record.document_id in excluded or record.amount is None:
            continue
        total += record.amount
        counted.append(ids[record.document_id].reference)
        if confirming is None and total >= minimum:
            confirming = record

    return MinimumPaymentResponse(
        met=confirming is not None,
        minimumAmount=minimum,
        totalCounted=total,
        confirmingReference=ids[confirming.document_id].reference if confirming else None,
        confirmingDate=confirming.receipt_date if confirming else None,
        countedReferences=counted,
        duplicateReferences=[ids[document_id].reference for document_id in excluded],
    )


def _tenant_minimum(connection: Connection, tenant_id: str) -> Decimal:
    """The tenant's configured minimum, read exactly as the Booking rule reads it."""
    from audit_core.uc03_booking_confirmation_rules import _minimum_booking_amount

    try:
        return _minimum_booking_amount(connection, tenant_id=tenant_id)
    except AuditCoreError:
        raise
