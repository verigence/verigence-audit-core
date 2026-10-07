"""06-Sep-2026 UC03 simplified Booking flow.

Approved authority: UC03_SIMPLIFICATION_DECISION_2026-09-06.md (C-01..C-07).

This patch deliberately keeps the existing DI/R2 structure intact. It removes the
PC customer-name dependency, uses the generated Journey ID in the existing customer
reference/display slot, and lets Review submit Booking without the removed manual
Booking Details payload. Existing DI-populated canonical values are never replaced
by NULL/manual placeholders.
"""
from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, status
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection

from audit_core import uc03_create_booking as create_booking
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_document_capture_v2 import (
    _base_requirements,
    _build_local_capture_response,
    _declarations,
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
    #
    # Folds the checklist/counters read (otherwise the workspace's own,
    # separate first GET /booking/capture round trip) into this same
    # response -- reported live as a visible lag on the counters after
    # opening a brand-new booking. A fresh booking has no documents yet, so
    # this is a handful of cheap, deterministic reads (config-driven
    # requirements, no DI/Security calls), not a second network hop.
    journey_id = UUID(str(body["journeyId"]))
    capture = _build_local_capture_response(
        connection=connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirements=_base_requirements(connection, tenant_id, journey_id),
        declaration_rows=_declarations(connection, tenant_id, journey_id),
        audit_documents=_linked_documents(connection, tenant_id, journey_id),
    )
    return create_booking.CreateBookingResponse.model_validate(
        {**body, "booking": capture.model_dump(mode="json")}
    )




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
    if getattr(create_booking, "_simplified_booking_flow_installed", False):
        return

    _replace_route(
        create_booking.router,
        suffix="/bookings",
        method="POST",
        endpoint=create_booking_journey_first_reference,
        response_model=create_booking.CreateBookingResponse,
        status_code=status.HTTP_201_CREATED,
    )
    create_booking._simplified_booking_flow_installed = True
