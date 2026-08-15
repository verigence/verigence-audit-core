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

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["customers"])


class CustomerCreate(BaseModel):
    customerTypeCode: str = Field(min_length=1, max_length=80)
    displayName: str = Field(min_length=1, max_length=240)
    mobileLast4: str | None = Field(default=None, min_length=4, max_length=4)
    emailReference: str | None = Field(default=None, max_length=240)
    externalCustomerRef: str | None = Field(default=None, max_length=160)


class CustomerPatch(BaseModel):
    customerTypeCode: str | None = Field(default=None, min_length=1, max_length=80)
    displayName: str | None = Field(default=None, min_length=1, max_length=240)
    mobileLast4: str | None = Field(default=None, min_length=4, max_length=4)
    emailReference: str | None = Field(default=None, max_length=240)
    externalCustomerRef: str | None = Field(default=None, max_length=160)
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class CustomerResponse(BaseModel):
    customerId: UUID
    dealerId: UUID
    outletId: UUID
    customerTypeCode: str
    displayName: str
    mobileLast4: str | None
    emailReference: str | None
    externalCustomerRef: str | None
    status: str


def _scope(connection: Connection, principal: Principal, tenant_id: str) -> None:
    require_tenant(principal, tenant_id)
    set_tenant_context(connection, tenant_id)


def _customer_response(row) -> CustomerResponse:
    return CustomerResponse(
        customerId=row["customer_id"],
        dealerId=row["dealer_id"],
        outletId=row["outlet_id"],
        customerTypeCode=row["customer_type_code"],
        displayName=row["display_name"],
        mobileLast4=row["mobile_last4"],
        emailReference=row["email_reference"],
        externalCustomerRef=row["external_customer_ref"],
        status=row["status"],
    )


def _dealer_for_outlet(connection: Connection, tenant_id: str, outlet_id: UUID) -> UUID:
    dealer_id = connection.execute(
        text(
            "SELECT dealer_id FROM auditcore.dealer_outlets "
            "WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id"
        ),
        {"tenant_id": tenant_id, "outlet_id": outlet_id},
    ).scalar_one_or_none()
    if dealer_id is None:
        raise NotFoundError(
            error_code="VAC-NF-003",
            title="Outlet not found",
            detail="Outlet not found for the requested tenant.",
        )
    return dealer_id


@router.post(
    "/outlets/{outlet_id}/customers",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_customer(
    tenant_id: str,
    outlet_id: UUID,
    payload: CustomerCreate,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> CustomerResponse:
    _scope(connection, principal, tenant_id)
    dealer_id = _dealer_for_outlet(connection, tenant_id, outlet_id)
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.customers (
                tenant_id, dealer_id, outlet_id, customer_type_code,
                display_name, mobile_last4, email_reference,
                external_customer_ref, created_by_actor_id
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_id, :customer_type_code,
                :display_name, :mobile_last4, :email_reference,
                :external_customer_ref, :actor_id
            )
            RETURNING customer_id, dealer_id, outlet_id, customer_type_code,
                      display_name, mobile_last4, email_reference,
                      external_customer_ref, status
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "customer_type_code": payload.customerTypeCode,
            "display_name": payload.displayName,
            "mobile_last4": payload.mobileLast4,
            "email_reference": payload.emailReference,
            "external_customer_ref": payload.externalCustomerRef,
            "actor_id": principal.subject,
        },
    ).mappings().one()
    return _customer_response(row)


@router.get("/outlets/{outlet_id}/customers", response_model=list[CustomerResponse])
def list_customers(
    tenant_id: str,
    outlet_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[CustomerResponse]:
    _scope(connection, principal, tenant_id)
    _dealer_for_outlet(connection, tenant_id, outlet_id)
    rows = connection.execute(
        text(
            """
            SELECT customer_id, dealer_id, outlet_id, customer_type_code,
                   display_name, mobile_last4, email_reference,
                   external_customer_ref, status
            FROM auditcore.customers
            WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id
            ORDER BY created_at_utc, customer_id
            """
        ),
        {"tenant_id": tenant_id, "outlet_id": outlet_id},
    ).mappings()
    return [_customer_response(row) for row in rows]


@router.get("/customers/{customer_id}", response_model=CustomerResponse)
def get_customer(
    tenant_id: str,
    customer_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> CustomerResponse:
    _scope(connection, principal, tenant_id)
    row = connection.execute(
        text(
            """
            SELECT customer_id, dealer_id, outlet_id, customer_type_code,
                   display_name, mobile_last4, email_reference,
                   external_customer_ref, status
            FROM auditcore.customers
            WHERE tenant_id = :tenant_id AND customer_id = :customer_id
            """
        ),
        {"tenant_id": tenant_id, "customer_id": customer_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-004",
            title="Customer not found",
            detail="Customer not found for the requested tenant.",
        )
    return _customer_response(row)


@router.patch("/customers/{customer_id}", response_model=CustomerResponse)
def patch_customer(
    tenant_id: str,
    customer_id: UUID,
    payload: CustomerPatch,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> CustomerResponse:
    _scope(connection, principal, tenant_id)
    row = connection.execute(
        text(
            """
            UPDATE auditcore.customers
            SET customer_type_code = COALESCE(:customer_type_code, customer_type_code),
                display_name = COALESCE(:display_name, display_name),
                mobile_last4 = COALESCE(:mobile_last4, mobile_last4),
                email_reference = COALESCE(:email_reference, email_reference),
                external_customer_ref = COALESCE(:external_customer_ref, external_customer_ref),
                status = COALESCE(:status, status),
                updated_by_actor_id = :actor_id,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id AND customer_id = :customer_id
            RETURNING customer_id, dealer_id, outlet_id, customer_type_code,
                      display_name, mobile_last4, email_reference,
                      external_customer_ref, status
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "customer_type_code": payload.customerTypeCode,
            "display_name": payload.displayName,
            "mobile_last4": payload.mobileLast4,
            "email_reference": payload.emailReference,
            "external_customer_ref": payload.externalCustomerRef,
            "status": payload.status,
            "actor_id": principal.subject,
        },
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-004",
            title="Customer not found",
            detail="Customer not found for the requested tenant.",
        )
    return _customer_response(row)
