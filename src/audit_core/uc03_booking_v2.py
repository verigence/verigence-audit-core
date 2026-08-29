from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, ConflictError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _require_active_booking, _scope
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _parse_if_match,
)
from audit_core.uc03_booking_details import (
    BookingDetailsCommand,
    BookingDetailsView,
    _MASTER_DOMAINS,
    _details_view,
    _effective_date,
    _validate_master,
    _validate_price_list,
)
from audit_core.uc03_document_capture_v2 import (
    _base_requirements,
    _build_local_capture_response,
    _capture_phase_state,
    _declarations,
    _linked_documents,
)

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}",
    tags=["uc03-booking-v2"],
)


class BookingSubmitV2Response(BaseModel):
    journeyId: UUID
    phase: Literal["BOOKING"] = "BOOKING"
    status: Literal["COMPLETED"] = "COMPLETED"
    pcVerificationStatus: Literal["PENDING"] = "PENDING"
    aggregateVersion: int


@router.get("/booking/details", response_model=BookingDetailsView)
def get_booking_details_v2(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingDetailsView:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _details_view(connection, tenant_id=tenant_id, journey_id=journey_id)


def _validate_declaration_alignment(
    command: BookingDetailsCommand,
    declarations: dict[str, dict[str, Any]],
) -> None:
    expected = {
        "exchangeTaken": command.tradeIn,
        "gstApplicable": command.gstBenefit,
        "corporateCustomer": command.customerType.strip().upper() == "CORPORATE",
    }
    labels = {
        "exchangeTaken": "Trade-In",
        "gstApplicable": "GST applicability",
        "corporateCustomer": "Corporate customer",
    }
    for condition_key, submitted in expected.items():
        declaration = declarations.get(condition_key)
        if declaration is None:
            continue
        if bool(declaration["applicable"]) != submitted:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Booking details conflict with Documents",
                detail=f"{labels[condition_key]} must match the answer captured on Documents.",
            )

    corporate = declarations.get("corporateCustomer")
    if corporate and bool(corporate["applicable"]):
        availability = corporate.get("document_available")
        if availability is not None and command.corporateIdAvailable is not None:
            if bool(availability) != bool(command.corporateIdAvailable):
                raise AuditCoreError(
                    error_code="VAC-VAL-002",
                    status_code=422,
                    title="Booking details conflict with Documents",
                    detail="Corporate ID availability must match the answer captured on Documents.",
                )


def _validated_details(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    command: BookingDetailsCommand,
) -> dict[str, str]:
    effective_on = _effective_date(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    if command.priceListId is not None:
        _validate_price_list(
            connection,
            tenant_id=tenant_id,
            price_list_id=command.priceListId,
            effective_on=effective_on,
        )
    return {
        field_name: _validate_master(
            connection,
            tenant_id=tenant_id,
            domain=domain,
            value=getattr(command, field_name),
            effective_on=effective_on,
            label=field_name,
        )
        for field_name, domain in _MASTER_DOMAINS.items()
    }


def _persist_details(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    command: BookingDetailsCommand,
    values: dict[str, str],
) -> None:
    customer_id = connection.execute(
        text(
            "SELECT customer_id FROM auditcore.journeys "
            "WHERE tenant_id=:tenant_id AND journey_id=:journey_id"
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            UPDATE auditcore.customers
            SET customer_type_code=:customer_type,
                updated_at_utc=now(), version_no=version_no+1
            WHERE tenant_id=:tenant_id AND customer_id=:customer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "customer_type": values["customerType"],
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.bookings (
                tenant_id, journey_id, price_list_id, deal_type_code,
                deal_source_code, lead_source_code, outright_purchase,
                corporate_id_available, gst_benefit
            ) VALUES (
                :tenant_id, :journey_id, :price_list_id, :deal_type,
                :deal_source, :lead_source, :outright_purchase,
                :corporate_id_available, :gst_benefit
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                price_list_id=EXCLUDED.price_list_id,
                deal_type_code=EXCLUDED.deal_type_code,
                deal_source_code=EXCLUDED.deal_source_code,
                lead_source_code=EXCLUDED.lead_source_code,
                outright_purchase=EXCLUDED.outright_purchase,
                corporate_id_available=EXCLUDED.corporate_id_available,
                gst_benefit=EXCLUDED.gst_benefit,
                updated_at_utc=now(),
                version_no=auditcore.bookings.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "price_list_id": command.priceListId,
            "deal_type": values["dealType"],
            "deal_source": values["dealSource"],
            "lead_source": values["leadSource"],
            "outright_purchase": command.outrightPurchase,
            "corporate_id_available": (
                command.corporateIdAvailable
                if values["customerType"] == "CORPORATE"
                else None
            ),
            "gst_benefit": command.gstBenefit,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.registration_records (
                tenant_id, journey_id, registration_state,
                registration_territory, registration_district,
                registration_type_code, registration_category_code,
                source_kind
            ) VALUES (
                :tenant_id, :journey_id, :registration_state,
                :territory, :district, :registration_type,
                :registration_category, 'OPERATIONAL_INPUT'
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                registration_state=EXCLUDED.registration_state,
                registration_territory=EXCLUDED.registration_territory,
                registration_district=EXCLUDED.registration_district,
                registration_type_code=EXCLUDED.registration_type_code,
                registration_category_code=EXCLUDED.registration_category_code,
                source_kind='OPERATIONAL_INPUT', source_evidence_id=NULL,
                updated_at_utc=now(),
                version_no=auditcore.registration_records.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "registration_state": values["registrationState"],
            "territory": values["territoryCategorization"],
            "district": values["districtName"],
            "registration_type": values["registrationType"],
            "registration_category": values["registrationCategory"],
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.trade_in_cases (
                tenant_id, journey_id, actual_status_code, source_kind
            ) VALUES (
                :tenant_id, :journey_id, :status_code, 'OPERATIONAL_INPUT'
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                actual_status_code=EXCLUDED.actual_status_code,
                source_kind='OPERATIONAL_INPUT', source_evidence_id=NULL,
                updated_at_utc=now(),
                version_no=auditcore.trade_in_cases.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "status_code": "EXCHANGE_TAKEN" if command.tradeIn else "NO_EXCHANGE",
        },
    )


@router.post("/booking/submit", response_model=BookingSubmitV2Response)
def submit_booking_v2(
    tenant_id: str,
    journey_id: UUID,
    command: BookingDetailsCommand,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingSubmitV2Response:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _capture_phase_state(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            for_update=True,
        )
        _require_active_booking(state)
        if int(state["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Booking version conflict",
                detail="Booking changed since it was loaded. Refresh the Booking and retry.",
            )
        if state["capture_completed_at_utc"] is not None:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking capture is complete",
                detail="Booking V2 capture has already been submitted.",
            )

        declarations = _declarations(connection, tenant_id, journey_id)
        _validate_declaration_alignment(command, declarations)
        local_capture = _build_local_capture_response(
            journey_id=journey_id,
            requirements=_base_requirements(connection, tenant_id, journey_id),
            declaration_rows=declarations,
            audit_documents=_linked_documents(connection, tenant_id, journey_id),
        )
        if not local_capture.canContinue:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking document capture is incomplete",
                detail="Required classifications or applicability decisions are still pending.",
            )

        values = _validated_details(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            command=command,
        )
        _persist_details(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            command=command,
            values=values,
        )

        next_version = expected_version + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status='BOOKING_IN_PROGRESS',
                    audit_state=CASE
                        WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS'
                        ELSE audit_state
                    END,
                    capture_completed_at_utc=now(),
                    pc_verification_status='PENDING',
                    latest_activity_at_utc=now(),
                    updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "version": next_version,
            },
        )
        _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_BOOKING_CAPTURE_SUBMITTED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(request),
            safe_payload={
                "capturePath": "V2_SINGLE_SUBMIT",
                "pcVerificationStatus": "PENDING",
                "customerType": values["customerType"],
                "tradeIn": command.tradeIn,
                "gstBenefit": command.gstBenefit,
                "bookingBusinessStatusChanged": False,
            },
            aggregate_version=next_version,
        )
        return BookingSubmitV2Response(
            journeyId=journey_id,
            aggregateVersion=next_version,
        ).model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking-v2.submit:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "details": command.model_dump(mode="json"),
        },
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return BookingSubmitV2Response.model_validate(body)
