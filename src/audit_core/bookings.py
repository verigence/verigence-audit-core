from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.product_catalogue import resolve_sellable_configuration
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["booking"])


class BookingUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bookingReference: str | None = Field(default=None, max_length=160)
    bookingDate: date | None = None
    salesStaffId: UUID
    productSkuId: UUID
    selectionSource: Literal["EVIDENCE", "OPERATIONAL_INPUT", "SOURCE_SYSTEM"] | None = None


class ProductSnapshot(BaseModel):
    productSkuId: UUID
    modelCode: str
    modelName: str
    variantCode: str
    variantName: str
    colourCode: str | None
    colourName: str | None
    selectionSource: str | None


class BookingResponse(BaseModel):
    bookingId: UUID
    journeyId: UUID
    bookingReference: str | None
    bookingDate: date | None
    salesStaffId: UUID
    product: ProductSnapshot


def _journey_scope(connection: Connection, tenant_id: str, journey_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Journey not found",
            detail="Journey not found for the requested tenant.",
        )
    return row


def _require_sales_consultant(
    connection: Connection,
    *,
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
    sales_staff_id: UUID,
) -> None:
    found = connection.execute(
        text(
            """
            SELECT 1
            FROM auditcore.dealership_staff
            WHERE tenant_id = :tenant_id
              AND dealer_id = :dealer_id
              AND outlet_id = :outlet_id
              AND dealership_staff_id = :sales_staff_id
              AND status = 'ACTIVE'
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "sales_staff_id": sales_staff_id,
        },
    ).scalar_one_or_none()
    if found is None:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Invalid sales consultant reference",
            detail="Sales consultant must be active in the Journey outlet.",
        )


def _response(connection: Connection, tenant_id: str, journey_id: UUID) -> BookingResponse:
    row = connection.execute(
        text(
            """
            SELECT b.booking_id, b.journey_id, b.booking_reference, b.booking_date,
                   b.sales_staff_id, jp.product_sku_id,
                   jp.model_code_snapshot, jp.model_name_snapshot,
                   jp.variant_code_snapshot, jp.variant_name_snapshot,
                   jp.colour_code_snapshot, jp.colour_name_snapshot,
                   jp.selection_source
            FROM auditcore.bookings b
            JOIN auditcore.journey_products jp
              ON jp.tenant_id = b.tenant_id AND jp.journey_id = b.journey_id
            WHERE b.tenant_id = :tenant_id AND b.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Booking not found",
            detail="Booking not found for the requested Journey.",
        )
    return BookingResponse(
        bookingId=row["booking_id"],
        journeyId=row["journey_id"],
        bookingReference=row["booking_reference"],
        bookingDate=row["booking_date"],
        salesStaffId=row["sales_staff_id"],
        product=ProductSnapshot(
            productSkuId=row["product_sku_id"],
            modelCode=row["model_code_snapshot"],
            modelName=row["model_name_snapshot"],
            variantCode=row["variant_code_snapshot"],
            variantName=row["variant_name_snapshot"],
            colourCode=row["colour_code_snapshot"],
            colourName=row["colour_name_snapshot"],
            selectionSource=row["selection_source"],
        ),
    )


@router.put("/journeys/{journey_id}/booking", response_model=BookingResponse)
def upsert_booking(
    tenant_id: str,
    journey_id: UUID,
    payload: BookingUpsert,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.update")
    set_tenant_context(connection, tenant_id)
    journey = _journey_scope(connection, tenant_id, journey_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )
    _require_sales_consultant(
        connection,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
        sales_staff_id=payload.salesStaffId,
    )
    product = resolve_sellable_configuration(connection, product_sku_id=payload.productSkuId)

    connection.execute(
        text(
            """
            INSERT INTO auditcore.bookings (
                tenant_id, journey_id, booking_reference, booking_date, sales_staff_id
            ) VALUES (
                :tenant_id, :journey_id, :booking_reference, :booking_date, :sales_staff_id
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                booking_reference = EXCLUDED.booking_reference,
                booking_date = EXCLUDED.booking_date,
                sales_staff_id = EXCLUDED.sales_staff_id,
                updated_at_utc = now(),
                version_no = auditcore.bookings.version_no + 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "booking_reference": payload.bookingReference,
            "booking_date": payload.bookingDate,
            "sales_staff_id": payload.salesStaffId,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_products (
                tenant_id, journey_id, product_sku_id,
                model_code_snapshot, model_name_snapshot,
                variant_code_snapshot, variant_name_snapshot,
                colour_code_snapshot, colour_name_snapshot, selection_source
            ) VALUES (
                :tenant_id, :journey_id, :product_sku_id,
                :model_code, :model_name, :variant_code, :variant_name,
                :colour_code, :colour_name, :selection_source
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                product_sku_id = EXCLUDED.product_sku_id,
                model_code_snapshot = EXCLUDED.model_code_snapshot,
                model_name_snapshot = EXCLUDED.model_name_snapshot,
                variant_code_snapshot = EXCLUDED.variant_code_snapshot,
                variant_name_snapshot = EXCLUDED.variant_name_snapshot,
                colour_code_snapshot = EXCLUDED.colour_code_snapshot,
                colour_name_snapshot = EXCLUDED.colour_name_snapshot,
                selection_source = EXCLUDED.selection_source,
                updated_at_utc = now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "product_sku_id": payload.productSkuId,
            "model_code": product["model_code"],
            "model_name": product["model_name"],
            "variant_code": product["variant_code"],
            "variant_name": product["variant_name"],
            "colour_code": product["colour_code"],
            "colour_name": product["colour_name"],
            "selection_source": payload.selectionSource,
        },
    )
    return _response(connection, tenant_id, journey_id)


@router.get("/journeys/{journey_id}/booking", response_model=BookingResponse)
def get_booking(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.read")
    set_tenant_context(connection, tenant_id)
    journey = _journey_scope(connection, tenant_id, journey_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )
    return _response(connection, tenant_id, journey_id)
