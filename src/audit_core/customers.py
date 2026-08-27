from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.authorization import require_tenant
from audit_core.customer_matching import find_customer_matches
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["customers"])

_FULL_CONTACT_PERMISSION = "audit.customer.contact.full.read"
_MASK_PREFIX = "******"


class CustomerCreate(BaseModel):
    customerTypeCode: str = Field(min_length=1, max_length=80)
    displayName: str = Field(min_length=1, max_length=240)
    mobileNumber: str | None = Field(default=None, min_length=4, max_length=40)
    mobileLast4: str | None = Field(default=None, min_length=4, max_length=4, pattern=r"^[0-9]{4}$")
    emailReference: str | None = Field(default=None, max_length=240)
    externalCustomerRef: str | None = Field(default=None, max_length=160)


class CustomerPatch(BaseModel):
    customerTypeCode: str | None = Field(default=None, min_length=1, max_length=80)
    displayName: str | None = Field(default=None, min_length=1, max_length=240)
    mobileNumber: str | None = Field(default=None, min_length=4, max_length=40)
    mobileLast4: str | None = Field(default=None, min_length=4, max_length=4, pattern=r"^[0-9]{4}$")
    emailReference: str | None = Field(default=None, max_length=240)
    externalCustomerRef: str | None = Field(default=None, max_length=160)
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class CustomerResponse(BaseModel):
    customerId: UUID
    dealerId: UUID
    outletId: UUID
    customerTypeCode: str
    displayName: str
    mobileNumber: str | None
    mobileLast4: str | None
    emailReference: str | None
    externalCustomerRef: str | None
    status: str


class CustomerMatchResponse(BaseModel):
    customerId: UUID
    dealerId: UUID
    outletId: UUID
    displayName: str
    identityType: str


def normalize_mobile_number(value: str) -> tuple[str, str]:
    """Normalize a mobile without inventing a country code.

    Formatting characters are removed. An explicit leading '+' is preserved. Audit
    Core deliberately does not infer +91 (or any other country code) from a domestic
    number because that business rule is not part of the approved UC03 baseline.
    """

    raw = value.strip()
    if not raw:
        raise ValueError("Customer mobile number cannot be blank")
    has_plus = raw.startswith("+")
    digits = "".join(character for character in raw if character.isdigit())
    if len(digits) < 4 or len(digits) > 20:
        raise ValueError("Customer mobile number must contain between 4 and 20 digits")
    normalized = f"+{digits}" if has_plus else digits
    return normalized, digits[-4:]


def _mobile_fields(
    mobile_number: str | None,
    mobile_last4: str | None,
) -> tuple[str | None, str | None]:
    if mobile_number is None:
        return None, mobile_last4
    try:
        normalized, derived_last4 = normalize_mobile_number(mobile_number)
    except ValueError as exc:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail=str(exc),
        ) from exc
    if mobile_last4 is not None and mobile_last4 != derived_last4:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail="mobileLast4 must match the final four digits of mobileNumber.",
        )
    return normalized, derived_last4


def _scope(connection: Connection, principal: Principal, tenant_id: str) -> None:
    require_tenant(principal, tenant_id)
    set_tenant_context(connection, tenant_id)


def _visible_mobile(row, principal: Principal) -> str | None:
    full_value = row["mobile_number"]
    if full_value is None:
        return None
    if _FULL_CONTACT_PERMISSION in principal.permissions:
        return full_value
    last4 = row["mobile_last4"]
    if last4 is None:
        _, last4 = normalize_mobile_number(full_value)
    return f"{_MASK_PREFIX}{last4}"


def _customer_response(row, principal: Principal) -> CustomerResponse:
    return CustomerResponse(
        customerId=row["customer_id"],
        dealerId=row["dealer_id"],
        outletId=row["outlet_id"],
        customerTypeCode=row["customer_type_code"],
        displayName=row["display_name"],
        mobileNumber=_visible_mobile(row, principal),
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
    mobile_number, mobile_last4 = _mobile_fields(payload.mobileNumber, payload.mobileLast4)
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.customers (
                tenant_id, dealer_id, outlet_id, customer_type_code,
                display_name, mobile_number, mobile_last4, email_reference,
                external_customer_ref, created_by_actor_id
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_id, :customer_type_code,
                :display_name, :mobile_number, :mobile_last4, :email_reference,
                :external_customer_ref, :actor_id
            )
            RETURNING customer_id, dealer_id, outlet_id, customer_type_code,
                      display_name, mobile_number, mobile_last4, email_reference,
                      external_customer_ref, status
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "customer_type_code": payload.customerTypeCode,
            "display_name": payload.displayName,
            "mobile_number": mobile_number,
            "mobile_last4": mobile_last4,
            "email_reference": payload.emailReference,
            "external_customer_ref": payload.externalCustomerRef,
            "actor_id": principal.subject,
        },
    ).mappings().one()
    return _customer_response(row, principal)


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
                   display_name, mobile_number, mobile_last4, email_reference,
                   external_customer_ref, status
            FROM auditcore.customers
            WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id
            ORDER BY created_at_utc, customer_id
            """
        ),
        {"tenant_id": tenant_id, "outlet_id": outlet_id},
    ).mappings()
    return [_customer_response(row, principal) for row in rows]


@router.get("/customers/matches", response_model=list[CustomerMatchResponse])
def match_customers(
    tenant_id: str,
    identity_type: Annotated[str, Query(alias="identityType", min_length=1, max_length=40)],
    match_hash: Annotated[str, Query(alias="matchHash", min_length=1, max_length=256)],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[CustomerMatchResponse]:
    _scope(connection, principal, tenant_id)
    rows = find_customer_matches(
        connection,
        tenant_id=tenant_id,
        identity_type=identity_type,
        match_hash=match_hash,
    )
    return [
        CustomerMatchResponse(
            customerId=row["customer_id"],
            dealerId=row["dealer_id"],
            outletId=row["outlet_id"],
            displayName=row["display_name"],
            identityType=row["identity_type"],
        )
        for row in rows
    ]


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
                   display_name, mobile_number, mobile_last4, email_reference,
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
    return _customer_response(row, principal)


@router.patch("/customers/{customer_id}", response_model=CustomerResponse)
def patch_customer(
    tenant_id: str,
    customer_id: UUID,
    payload: CustomerPatch,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> CustomerResponse:
    _scope(connection, principal, tenant_id)
    mobile_number, derived_last4 = _mobile_fields(payload.mobileNumber, payload.mobileLast4)
    row = connection.execute(
        text(
            """
            UPDATE auditcore.customers
            SET customer_type_code = COALESCE(:customer_type_code, customer_type_code),
                display_name = COALESCE(:display_name, display_name),
                mobile_number = COALESCE(:mobile_number, mobile_number),
                mobile_last4 = CASE
                    WHEN :mobile_number IS NOT NULL THEN :derived_last4
                    WHEN mobile_number IS NULL THEN COALESCE(:mobile_last4, mobile_last4)
                    ELSE mobile_last4
                END,
                email_reference = COALESCE(:email_reference, email_reference),
                external_customer_ref = COALESCE(:external_customer_ref, external_customer_ref),
                status = COALESCE(:status, status),
                updated_by_actor_id = :actor_id,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id AND customer_id = :customer_id
            RETURNING customer_id, dealer_id, outlet_id, customer_type_code,
                      display_name, mobile_number, mobile_last4, email_reference,
                      external_customer_ref, status
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "customer_type_code": payload.customerTypeCode,
            "display_name": payload.displayName,
            "mobile_number": mobile_number,
            "derived_last4": derived_last4,
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
    return _customer_response(row, principal)
