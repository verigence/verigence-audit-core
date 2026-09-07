"""06-Sep-2026 UC03 simplified Booking flow.

Approved authority: UC03_SIMPLIFICATION_DECISION_2026-09-06.md (C-01..C-07).

This patch deliberately keeps the existing DI/R2 structure intact. It removes the
PC customer-name dependency, uses the generated Journey ID in the existing customer
reference/display slot, and lets Review submit Booking without the removed manual
Booking Details payload. Existing DI-populated canonical values are never replaced
by NULL/manual placeholders.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, Header, Request, Response, status
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core import uc03_booking_v2 as booking_v2
from audit_core import uc03_create_booking as create_booking
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
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _parse_if_match,
)
from audit_core.uc03_confidence_review_policy import _unreviewed_low_confidence_count
from audit_core.uc03_document_capture_v2 import (
    _base_requirements,
    _capture_phase_state,
    _linked_documents,
)
from audit_core.uc03_simplified_create_atomic import (
    execute_simplified_create_booking_atomic,
)


class SimplifiedCreateBookingCommand(BaseModel):
    model_config = ConfigDict(extra="ignore")

    outletId: UUID
    # Compatibility only. The active 06-Sep flow never asks PC for this value and
    # any legacy value supplied here is deliberately ignored.
    customerName: str | None = None


class SimplifiedBookingSubmitCommand(BaseModel):
    # Old V2 clients may still send removed Booking Details fields during rollout.
    # They are ignored rather than written, so they cannot overwrite DI-populated Core.
    model_config = ConfigDict(extra="ignore")


class SimplifiedBookingSubmitResponse(BaseModel):
    journeyId: UUID
    phase: Literal["BOOKING"] = "BOOKING"
    status: Literal["IN_PROGRESS", "COMPLETED"]
    pcVerificationStatus: Literal["PENDING", "VERIFIED"] | None = None
    aggregateVersion: int


def create_booking_journey_first_reference(
    tenant_id: str,
    payload: SimplifiedCreateBookingCommand,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> create_booking.CreateBookingResponse:
    create_booking._authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    create_booking.set_tenant_context(connection, tenant_id)
    context = create_booking._create_context(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        outlet_id=payload.outletId,
    )

    # The Journey UUID is generated inside the same atomic SQL statement before
    # Customer insert. It is therefore the Customer reference from row creation;
    # no post-create Customer mutation is needed or permitted.
    request_payload = {
        "outletId": str(payload.outletId),
        "referenceMode": "JOURNEY_ID",
    }
    body = execute_simplified_create_booking_atomic(
        connection,
        tenant_id=tenant_id,
        context=context,
        actor_id=human_principal.subject,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
    )

    # journey_workflow_events remains append-only; BOOKING_CREATED is written once
    # as part of the same atomic create statement.
    return create_booking.CreateBookingResponse.model_validate(body)


def submit_booking_from_review(
    tenant_id: str,
    journey_id: UUID,
    command: SimplifiedBookingSubmitCommand,
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
) -> SimplifiedBookingSubmitResponse:
    del command
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
                detail="Booking changed since Review was loaded. Refresh Review and retry.",
            )
        if state["capture_completed_at_utc"] is not None:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking capture is complete",
                detail="Booking V2 capture has already been completed.",
            )

        # Condition 2: facts already extracted below 90% must be reviewed before
        # submit. DI processing itself is not a submit blocker (Condition 1).
        low_confidence = _unreviewed_low_confidence_count(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        if low_confidence:
            raise ConflictError(
                error_code="VAC-CONFLICT-012",
                title="PC review is required before Booking submit",
                detail=(
                    f"Review {low_confidence} extracted DI field"
                    f"{'s' if low_confidence != 1 else ''} below 90% confidence before submitting Booking."
                ),
            )

        requirements = _base_requirements(connection, tenant_id, journey_id)
        documents = _linked_documents(connection, tenant_id, journey_id)
        mandatory_documents_complete = booking_v2._mandatory_booking_documents_complete(
            requirements,
            documents,
        )
        current_pc_status = connection.execute(
            text(
                """
                SELECT pc_verification_status
                FROM auditcore.journey_stage_states
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one_or_none()
        preserved_pc_status = (
            "VERIFIED" if str(current_pc_status or "").upper() == "VERIFIED" else "PENDING"
        ) if mandatory_documents_complete else None

        next_version = expected_version + 1
        business_status = (
            "BOOKING_CLOSED" if mandatory_documents_complete else "BOOKING_IN_PROGRESS"
        )
        closure_disposition = (
            "PROCEED_TO_DELIVERY" if mandatory_documents_complete else None
        )

        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status=:business_status,
                    closure_disposition=:closure_disposition,
                    audit_state=CASE
                        WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS'
                        ELSE audit_state
                    END,
                    capture_completed_at_utc=CASE
                        WHEN :mandatory_documents_complete THEN now()
                        ELSE NULL
                    END,
                    pc_verification_status=:pc_verification_status,
                    business_completed_at_utc=CASE
                        WHEN :mandatory_documents_complete THEN now()
                        ELSE NULL
                    END,
                    closed_at_utc=CASE
                        WHEN :mandatory_documents_complete THEN now()
                        ELSE NULL
                    END,
                    closed_by_actor_id=CASE
                        WHEN :mandatory_documents_complete THEN :actor_id
                        ELSE NULL
                    END,
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
                "actor_id": human_principal.subject,
                "version": next_version,
                "business_status": business_status,
                "closure_disposition": closure_disposition,
                "pc_verification_status": preserved_pc_status,
                "mandatory_documents_complete": mandatory_documents_complete,
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
                "capturePath": "V2_SIMPLIFIED_REVIEW_SUBMIT",
                "mandatoryDocumentsComplete": mandatory_documents_complete,
                "pcVerificationStatus": preserved_pc_status,
                "manualBookingDetailsCaptured": False,
                "bookingBusinessStatus": business_status,
                "closureDisposition": closure_disposition,
            },
            aggregate_version=next_version,
        )
        return SimplifiedBookingSubmitResponse(
            journeyId=journey_id,
            status="COMPLETED" if mandatory_documents_complete else "IN_PROGRESS",
            pcVerificationStatus=preserved_pc_status,
            aggregateVersion=next_version,
        ).model_dump(mode="json")

    # The idempotency key is scoped to journey+action only — not to expectedVersion.
    # Including the version in request_payload would cause retries with a refreshed
    # version (after a Refresh Review) to bypass the cache and re-execute, producing
    # a second VAC-CONFLICT-005 on the fresh version when the first execution had
    # already committed. The If-Match check inside execute() is the sole concurrency
    # guard; the stored payload is audit evidence only.
    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking-v2.simplified-submit:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"details": None},
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return SimplifiedBookingSubmitResponse.model_validate(body)


def _replace_route(
    router: Any,
    *,
    suffix: str,
    method: str,
    endpoint: Any,
    response_model: Any,
    status_code: int | None = None,
) -> None:
    router.routes[:] = [
        route
        for route in router.routes
        if not (
            isinstance(route, APIRoute)
            and route.path.endswith(suffix)
            and method in route.methods
        )
    ]
    kwargs: dict[str, Any] = {
        "methods": [method],
        "response_model": response_model,
    }
    if status_code is not None:
        kwargs["status_code"] = status_code
    router.add_api_route(suffix, endpoint, **kwargs)


def install_uc03_simplified_booking_flow() -> None:
    if getattr(booking_v2, "_simplified_booking_flow_installed", False):
        return

    _replace_route(
        create_booking.router,
        suffix="/bookings",
        method="POST",
        endpoint=create_booking_journey_first_reference,
        response_model=create_booking.CreateBookingResponse,
        status_code=status.HTTP_201_CREATED,
    )
    _replace_route(
        booking_v2.router,
        suffix="/booking/submit",
        method="POST",
        endpoint=submit_booking_from_review,
        response_model=SimplifiedBookingSubmitResponse,
    )
    booking_v2._simplified_booking_flow_installed = True
