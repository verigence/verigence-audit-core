from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core import uc03_booking_capture
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_commands import _authorize_security, _journey_context

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/booking",
    tags=["uc03-identity-business-date"],
)


class BookingIdentityBusinessDateView(BaseModel):
    journeyId: UUID
    enteredName: str
    legalName: str | None
    legalNameStatus: str
    legalNameSourceEvidenceId: UUID | None
    actualBookingDate: date | None
    auditCapturedAtUtc: datetime
    captureLagDays: int | None


_original_write_typed_capture = uc03_booking_capture._write_typed_capture
_installed = False


def _identity_aware_write_typed_capture(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    field_key: str,
    value: Any,
    source_evidence_id: UUID | None,
) -> tuple[str, str]:
    """Preserve Entered Name while reusing the existing proposal decision flow.

    PAN/Aadhaar proposals still resolve through CUSTOMER_NAME for backward
    compatibility with the existing generic proposal code, but this adapter stops
    that legacy target from overwriting customers.display_name. The migration's
    proposal-status trigger writes the accepted/corrected identity value to
    customers.legal_name instead.
    """
    key = field_key.strip().upper()
    if key == "CUSTOMER_NAME":
        customer_id = uc03_booking_capture._journey_customer_id(
            connection, tenant_id, journey_id
        )
        if source_evidence_id is None:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Entered Name is read-only",
                detail=(
                    "The Customer Name entered when the Booking Journey was created "
                    "is retained as audit input and cannot be edited afterward."
                ),
            )
        uc03_booking_capture._validate_evidence_for_journey(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_id=source_evidence_id,
        )
        return "CUSTOMER", str(customer_id)

    return _original_write_typed_capture(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        field_key=field_key,
        value=value,
        source_evidence_id=source_evidence_id,
    )


def install_uc03_identity_business_date() -> None:
    """Install the reconciled UC03 identity publication boundary.

    Booking-form customer_name remains a document fact but is not identity
    authoritative. PAN and Aadhaar names are the only current Legal Name sources.
    """
    global _installed
    if _installed:
        return

    for document_type in ("booking_form", "booking_docket"):
        fields = set(uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS.get(document_type, set()))
        fields.discard("customer_name")
        uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS[document_type] = fields

    uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS["aadhaar"] = {"aadhaar_name"}
    uc03_booking_capture._PROPOSAL_CAPTURE_MAP["aadhaar_name"] = "CUSTOMER_NAME"
    uc03_booking_capture._write_typed_capture = _identity_aware_write_typed_capture
    _installed = True


@router.get("/identity-context", response_model=BookingIdentityBusinessDateView)
def get_booking_identity_business_date(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingIdentityBusinessDateView:
    _authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    _journey_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )

    row = connection.execute(
        text(
            """
            SELECT
                j.journey_id,
                c.display_name AS entered_name,
                c.legal_name,
                c.legal_name_status,
                c.legal_name_source_evidence_id,
                b.booking_date AS actual_booking_date,
                j.created_at_utc AS audit_captured_at_utc,
                CASE
                    WHEN b.booking_date IS NULL THEN NULL
                    ELSE ((j.created_at_utc AT TIME ZONE p.timezone_name)::date - b.booking_date)
                END AS capture_lag_days
            FROM auditcore.journeys j
            JOIN auditcore.customers c
              ON c.tenant_id = j.tenant_id
             AND c.customer_id = j.customer_id
            JOIN auditcore.projects p
              ON p.tenant_id = j.tenant_id
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id = j.tenant_id
             AND b.journey_id = j.journey_id
            WHERE j.tenant_id = :tenant_id
              AND j.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Booking not found",
            detail="Booking case not found for the requested Project.",
        )

    return BookingIdentityBusinessDateView(
        journeyId=row["journey_id"],
        enteredName=row["entered_name"],
        legalName=row["legal_name"],
        legalNameStatus=row["legal_name_status"],
        legalNameSourceEvidenceId=row["legal_name_source_evidence_id"],
        actualBookingDate=row["actual_booking_date"],
        auditCapturedAtUtc=row["audit_captured_at_utc"],
        captureLagDays=row["capture_lag_days"],
    )
