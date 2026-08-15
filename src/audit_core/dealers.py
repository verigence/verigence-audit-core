from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.authorization import require_tenant
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["dealers"])


class DealerCreate(BaseModel):
    dealerCode: str = Field(min_length=1, max_length=100)
    dealerName: str = Field(min_length=1, max_length=240)
    legalName: str | None = Field(default=None, max_length=240)


class DealerPatch(BaseModel):
    dealerName: str | None = Field(default=None, min_length=1, max_length=240)
    legalName: str | None = Field(default=None, max_length=240)
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class DealerResponse(BaseModel):
    dealerId: UUID
    dealerCode: str
    dealerName: str
    legalName: str | None
    status: str


class OutletCreate(BaseModel):
    outletCode: str = Field(min_length=1, max_length=100)
    outletName: str = Field(min_length=1, max_length=240)
    outletClassification: Literal["ONSITE", "SATELLITE"] = "ONSITE"
    city: str | None = Field(default=None, max_length=160)
    stateRegion: str | None = Field(default=None, max_length=160)
    postalCode: str | None = Field(default=None, max_length=40)


class OutletPatch(BaseModel):
    outletName: str | None = Field(default=None, min_length=1, max_length=240)
    outletClassification: Literal["ONSITE", "SATELLITE"] | None = None
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class OutletResponse(BaseModel):
    outletId: UUID
    dealerId: UUID
    outletCode: str
    outletName: str
    outletClassification: str
    city: str | None
    stateRegion: str | None
    postalCode: str | None
    status: str


def _not_found(resource: str) -> NotFoundError:
    return NotFoundError(
        error_code="VAC-NF-002" if resource == "Dealer" else "VAC-NF-003",
        title=f"{resource} not found",
        detail=f"{resource} not found for the requested tenant hierarchy.",
    )


def _scope(connection: Connection, principal: Principal, tenant_id: str) -> None:
    require_tenant(principal, tenant_id)
    set_tenant_context(connection, tenant_id)


def _dealer_response(row) -> DealerResponse:
    return DealerResponse(
        dealerId=row["dealer_id"],
        dealerCode=row["dealer_code"],
        dealerName=row["dealer_name"],
        legalName=row["legal_name"],
        status=row["status"],
    )


def _outlet_response(row) -> OutletResponse:
    return OutletResponse(
        outletId=row["outlet_id"],
        dealerId=row["dealer_id"],
        outletCode=row["outlet_code"],
        outletName=row["outlet_name"],
        outletClassification=row["outlet_classification"],
        city=row["city"],
        stateRegion=row["state_region"],
        postalCode=row["postal_code"],
        status=row["status"],
    )


@router.post("/dealers", response_model=DealerResponse, status_code=status.HTTP_201_CREATED)
def create_dealer(
    tenant_id: str,
    payload: DealerCreate,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DealerResponse:
    _scope(connection, principal, tenant_id)
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.dealers (
                tenant_id, dealer_code, dealer_name, legal_name, created_by_actor_id
            ) VALUES (
                :tenant_id, :dealer_code, :dealer_name, :legal_name, :actor_id
            )
            RETURNING dealer_id, dealer_code, dealer_name, legal_name, status
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_code": payload.dealerCode,
            "dealer_name": payload.dealerName,
            "legal_name": payload.legalName,
            "actor_id": principal.subject,
        },
    ).mappings().one()
    return _dealer_response(row)


@router.get("/dealers", response_model=list[DealerResponse])
def list_dealers(
    tenant_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[DealerResponse]:
    _scope(connection, principal, tenant_id)
    rows = connection.execute(
        text(
            """
            SELECT dealer_id, dealer_code, dealer_name, legal_name, status
            FROM auditcore.dealers
            WHERE tenant_id = :tenant_id
            ORDER BY dealer_code
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings()
    return [_dealer_response(row) for row in rows]


@router.get("/dealers/{dealer_id}", response_model=DealerResponse)
def get_dealer(
    tenant_id: str,
    dealer_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DealerResponse:
    _scope(connection, principal, tenant_id)
    row = connection.execute(
        text(
            """
            SELECT dealer_id, dealer_code, dealer_name, legal_name, status
            FROM auditcore.dealers
            WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
            """
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id},
    ).mappings().one_or_none()
    if row is None:
        raise _not_found("Dealer")
    return _dealer_response(row)


@router.patch("/dealers/{dealer_id}", response_model=DealerResponse)
def patch_dealer(
    tenant_id: str,
    dealer_id: UUID,
    payload: DealerPatch,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DealerResponse:
    _scope(connection, principal, tenant_id)
    row = connection.execute(
        text(
            """
            UPDATE auditcore.dealers
            SET dealer_name = COALESCE(:dealer_name, dealer_name),
                legal_name = COALESCE(:legal_name, legal_name),
                status = COALESCE(:status, status),
                updated_by_actor_id = :actor_id,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
            RETURNING dealer_id, dealer_code, dealer_name, legal_name, status
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "dealer_name": payload.dealerName,
            "legal_name": payload.legalName,
            "status": payload.status,
            "actor_id": principal.subject,
        },
    ).mappings().one_or_none()
    if row is None:
        raise _not_found("Dealer")
    return _dealer_response(row)


@router.post(
    "/dealers/{dealer_id}/outlets",
    response_model=OutletResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_outlet(
    tenant_id: str,
    dealer_id: UUID,
    payload: OutletCreate,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> OutletResponse:
    _scope(connection, principal, tenant_id)
    dealer_exists = connection.execute(
        text(
            "SELECT 1 FROM auditcore.dealers "
            "WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id"
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id},
    ).scalar_one_or_none()
    if dealer_exists is None:
        raise _not_found("Dealer")

    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.dealer_outlets (
                tenant_id, dealer_id, outlet_code, outlet_name,
                outlet_classification, city, state_region, postal_code,
                created_by_actor_id
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_code, :outlet_name,
                :classification, :city, :state_region, :postal_code, :actor_id
            )
            RETURNING outlet_id, dealer_id, outlet_code, outlet_name,
                      outlet_classification, city, state_region, postal_code, status
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_code": payload.outletCode,
            "outlet_name": payload.outletName,
            "classification": payload.outletClassification,
            "city": payload.city,
            "state_region": payload.stateRegion,
            "postal_code": payload.postalCode,
            "actor_id": principal.subject,
        },
    ).mappings().one()
    return _outlet_response(row)


@router.get("/dealers/{dealer_id}/outlets", response_model=list[OutletResponse])
def list_outlets(
    tenant_id: str,
    dealer_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[OutletResponse]:
    _scope(connection, principal, tenant_id)
    rows = connection.execute(
        text(
            """
            SELECT outlet_id, dealer_id, outlet_code, outlet_name,
                   outlet_classification, city, state_region, postal_code, status
            FROM auditcore.dealer_outlets
            WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
            ORDER BY outlet_code
            """
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id},
    ).mappings()
    return [_outlet_response(row) for row in rows]


@router.get("/dealers/{dealer_id}/outlets/{outlet_id}", response_model=OutletResponse)
def get_outlet(
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> OutletResponse:
    _scope(connection, principal, tenant_id)
    row = connection.execute(
        text(
            """
            SELECT outlet_id, dealer_id, outlet_code, outlet_name,
                   outlet_classification, city, state_region, postal_code, status
            FROM auditcore.dealer_outlets
            WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
              AND outlet_id = :outlet_id
            """
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
    ).mappings().one_or_none()
    if row is None:
        raise _not_found("Outlet")
    return _outlet_response(row)


@router.patch("/dealers/{dealer_id}/outlets/{outlet_id}", response_model=OutletResponse)
def patch_outlet(
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
    payload: OutletPatch,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> OutletResponse:
    _scope(connection, principal, tenant_id)
    row = connection.execute(
        text(
            """
            UPDATE auditcore.dealer_outlets
            SET outlet_name = COALESCE(:outlet_name, outlet_name),
                outlet_classification = COALESCE(:classification, outlet_classification),
                status = COALESCE(:status, status),
                updated_by_actor_id = :actor_id,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
              AND outlet_id = :outlet_id
            RETURNING outlet_id, dealer_id, outlet_code, outlet_name,
                      outlet_classification, city, state_region, postal_code, status
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "outlet_name": payload.outletName,
            "classification": payload.outletClassification,
            "status": payload.status,
            "actor_id": principal.subject,
        },
    ).mappings().one_or_none()
    if row is None:
        raise _not_found("Outlet")
    return _outlet_response(row)
