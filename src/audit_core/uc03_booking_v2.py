from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import ConflictError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _require_active_booking, _scope
from audit_core.uc03_booking_commands import _append_workflow_event
from audit_core.uc03_booking_details import (
    BookingDetailsCommand,
    BookingDetailsView,
    _details_view,
    save_booking_details,
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


def _event_key(base: str, suffix: str) -> str:
    return f"{base[:180]}:{suffix}"


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
    expected_version = int(if_match.strip().removeprefix('W/').strip().strip('"'))

    def execute() -> dict[str, Any]:
        saved = save_booking_details(
            request=request,
            tenant_id=tenant_id,
            journey_id=journey_id,
            command=command,
            if_match=if_match,
            idempotency_key=_event_key(idempotency_key, "details"),
            human_principal=human_principal,
            authorization_client=authorization_client,
            connection=connection,
        )

        state = _capture_phase_state(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            for_update=True,
        )
        _require_active_booking(state)
        if int(state["version_no"]) != saved.aggregateVersion:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Booking version conflict",
                detail="Booking changed during submission. Refresh the Booking and retry.",
            )
        if state["capture_completed_at_utc"] is not None:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking capture is complete",
                detail="Booking V2 capture has already been submitted.",
            )

        local_capture = _build_local_capture_response(
            journey_id=journey_id,
            requirements=_base_requirements(connection, tenant_id, journey_id),
            declaration_rows=_declarations(connection, tenant_id, journey_id),
            audit_documents=_linked_documents(connection, tenant_id, journey_id),
        )
        if not local_capture.canContinue:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking document capture is incomplete",
                detail="Required classifications or applicability decisions are still pending.",
            )

        next_version = int(saved.aggregateVersion) + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET capture_completed_at_utc=now(),
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
        context = _scope(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            human_principal=human_principal,
            authorization_client=authorization_client,
        )
        _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_BOOKING_CAPTURE_SUBMITTED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=_event_key(idempotency_key, "complete"),
            correlation_id=get_correlation_id(request),
            safe_payload={
                "capturePath": "V2_SINGLE_SUBMIT",
                "pcVerificationStatus": "PENDING",
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
