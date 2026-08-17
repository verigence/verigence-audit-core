from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}/reference", tags=["reference-data"])


class StaffReferenceResponse(BaseModel):
    staffId: UUID
    dealerId: UUID
    outletId: UUID
    roleCode: str
    displayName: str
    employeeReference: str | None


class ProductSkuReferenceResponse(BaseModel):
    productSkuId: UUID
    skuCode: str
    oemCode: str
    oemName: str
    modelCode: str
    modelName: str
    variantCode: str
    variantName: str
    colourCode: str | None
    colourName: str | None


class StatusCodeReferenceResponse(BaseModel):
    domainKey: str
    statusCode: str
    statusLabel: str


def _outlet_scope(connection: Connection, tenant_id: str, outlet_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT dealer_id
            FROM auditcore.dealer_outlets
            WHERE tenant_id = :tenant_id
              AND outlet_id = :outlet_id
              AND status = 'ACTIVE'
            """
        ),
        {"tenant_id": tenant_id, "outlet_id": outlet_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-003",
            title="Outlet not found",
            detail="Outlet not found for the requested tenant.",
        )
    return row


@router.get("/outlets/{outlet_id}/staff", response_model=list[StaffReferenceResponse])
def list_outlet_staff(
    tenant_id: str,
    outlet_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
    role_code: Annotated[str | None, Query(alias="roleCode", max_length=80)] = None,
) -> list[StaffReferenceResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.master.read")
    set_tenant_context(connection, tenant_id)
    outlet = _outlet_scope(connection, tenant_id, outlet_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=outlet["dealer_id"],
        outlet_id=outlet_id,
    )
    rows = connection.execute(
        text(
            """
            SELECT dealership_staff_id, dealer_id, outlet_id, staff_role_code,
                   display_name, employee_reference
            FROM auditcore.dealership_staff
            WHERE tenant_id = :tenant_id
              AND outlet_id = :outlet_id
              AND status = 'ACTIVE'
              AND (:role_code IS NULL OR staff_role_code = :role_code)
            ORDER BY display_name, dealership_staff_id
            """
        ),
        {"tenant_id": tenant_id, "outlet_id": outlet_id, "role_code": role_code},
    ).mappings().all()
    return [
        StaffReferenceResponse(
            staffId=row["dealership_staff_id"],
            dealerId=row["dealer_id"],
            outletId=row["outlet_id"],
            roleCode=row["staff_role_code"],
            displayName=row["display_name"],
            employeeReference=row["employee_reference"],
        )
        for row in rows
    ]


@router.get("/product-skus", response_model=list[ProductSkuReferenceResponse])
def list_product_skus(
    tenant_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ProductSkuReferenceResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.master.read")
    set_tenant_context(connection, tenant_id)
    search = None if q is None or not q.strip() else f"%{q.strip()}%"
    rows = connection.execute(
        text(
            """
            SELECT s.product_sku_id, s.sku_code,
                   o.oem_code, o.oem_name,
                   m.model_code, m.model_name,
                   v.variant_code, v.variant_name,
                   c.colour_code, c.colour_name
            FROM auditcore.product_skus s
            JOIN auditcore.oems o ON o.oem_id = s.oem_id
            JOIN auditcore.product_models m ON m.model_id = s.model_id
            JOIN auditcore.product_variants v ON v.variant_id = s.variant_id
            LEFT JOIN auditcore.colours c ON c.colour_id = s.colour_id
            WHERE s.is_active AND o.is_active AND m.is_active AND v.is_active
              AND (c.colour_id IS NULL OR c.is_active)
              AND (
                    :search IS NULL
                    OR s.sku_code ILIKE :search
                    OR m.model_name ILIKE :search
                    OR v.variant_name ILIKE :search
                    OR COALESCE(c.colour_name, '') ILIKE :search
              )
            ORDER BY m.model_name, v.variant_name, c.colour_name, s.sku_code
            LIMIT :limit
            """
        ),
        {"search": search, "limit": limit},
    ).mappings().all()
    return [
        ProductSkuReferenceResponse(
            productSkuId=row["product_sku_id"],
            skuCode=row["sku_code"],
            oemCode=row["oem_code"],
            oemName=row["oem_name"],
            modelCode=row["model_code"],
            modelName=row["model_name"],
            variantCode=row["variant_code"],
            variantName=row["variant_name"],
            colourCode=row["colour_code"],
            colourName=row["colour_name"],
        )
        for row in rows
    ]


@router.get("/status-codes", response_model=list[StatusCodeReferenceResponse])
def list_status_codes(
    tenant_id: str,
    domain_key: Annotated[str, Query(alias="domainKey", min_length=1, max_length=80)],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[StatusCodeReferenceResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.master.read")
    set_tenant_context(connection, tenant_id)
    rows = connection.execute(
        text(
            """
            SELECT domain_key, status_code, status_label
            FROM auditcore.business_status_codes
            WHERE tenant_id = :tenant_id
              AND domain_key = :domain_key
              AND is_active = true
            ORDER BY status_label, status_code
            """
        ),
        {"tenant_id": tenant_id, "domain_key": domain_key.upper()},
    ).mappings().all()
    return [
        StatusCodeReferenceResponse(
            domainKey=row["domain_key"],
            statusCode=row["status_code"],
            statusLabel=row["status_label"],
        )
        for row in rows
    ]
