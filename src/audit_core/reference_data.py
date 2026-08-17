from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}/reference", tags=["reference-data"])


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
            JOIN auditcore.projects p
              ON p.tenant_id = :tenant_id
             AND p.oem_id = s.oem_id
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
        {"tenant_id": tenant_id, "search": search, "limit": limit},
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
