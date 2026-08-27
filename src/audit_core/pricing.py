from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import DependencyUnavailableError
from audit_core.price_lists import get_price_matrix, resolve_effective_price_plan
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/pricing", tags=["pricing"])
_PERMISSION_KEY = "audit.master.read"


class EffectivePricePlanResponse(BaseModel):
    priceListId: UUID
    priceListVersionId: UUID
    priceListCode: str
    priceListName: str
    versionNo: int
    effectiveFrom: date
    effectiveTo: date | None
    currencyCode: str


class PriceComponentResponse(BaseModel):
    componentKey: str
    standardAmount: Decimal
    taxInclusive: bool | None


class PriceMatrixResponse(BaseModel):
    priceListVersionId: UUID
    productSkuId: UUID
    currencyCode: str
    components: list[PriceComponentResponse]


def _authorize_pricing(
    client: SecurityAuthorizationClient,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=_PERMISSION_KEY,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="Price Master lookup is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


@router.get("/plan", response_model=EffectivePricePlanResponse)
def get_effective_price_plan(
    tenant_id: str,
    effective_on: Annotated[date, Query(alias="effectiveOn")],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> EffectivePricePlanResponse:
    _authorize_pricing(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    plan = resolve_effective_price_plan(
        connection,
        tenant_id=tenant_id,
        effective_on=effective_on,
    )
    return EffectivePricePlanResponse(
        priceListId=plan["price_list_id"],
        priceListVersionId=plan["price_list_version_id"],
        priceListCode=plan["price_list_code"],
        priceListName=plan["price_list_name"],
        versionNo=int(plan["version_no"]),
        effectiveFrom=plan["effective_from"],
        effectiveTo=plan["effective_to"],
        currencyCode=str(plan["currency_code"]),
    )


@router.get("/matrix", response_model=PriceMatrixResponse)
def get_runtime_price_matrix(
    tenant_id: str,
    price_list_version_id: Annotated[UUID, Query(alias="priceListVersionId")],
    product_sku_id: Annotated[UUID, Query(alias="productSkuId")],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> PriceMatrixResponse:
    _authorize_pricing(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    rows = get_price_matrix(
        connection,
        tenant_id=tenant_id,
        price_list_version_id=price_list_version_id,
        product_sku_id=product_sku_id,
    )
    return PriceMatrixResponse(
        priceListVersionId=price_list_version_id,
        productSkuId=product_sku_id,
        currencyCode=str(rows[0]["currency_code"]),
        components=[
            PriceComponentResponse(
                componentKey=row["component_key"],
                standardAmount=row["standard_amount"],
                taxInclusive=row["tax_inclusive"],
            )
            for row in rows
        ],
    )
