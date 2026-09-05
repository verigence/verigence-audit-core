from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import Connection

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_delivery_capture_v2 import (
    DeliveryCaptureV2Response,
    _authorize_delivery,
    _build_local_delivery_capture_response,
    _delivery_requirements,
    _linked_delivery_documents,
)
from audit_core.uc03_document_capture_v2 import (
    BookingCaptureV2Response,
    _base_requirements,
    _build_local_capture_response,
    _capture_phase_state,
    _declarations,
    _linked_documents,
)

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}",
    tags=["uc03-capture-local-reads"],
)


@router.get("/booking/capture-local", response_model=BookingCaptureV2Response)
def get_booking_capture_local_v2(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingCaptureV2Response:
    # This endpoint is a durable read model used to open existing Booking details.
    # It must remain readable after Booking completion/closure. The active-state
    # guard belongs only on capture/extraction mutations, not on a read-only view.
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    _capture_phase_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        for_update=False,
    )
    return _build_local_capture_response(
        journey_id=journey_id,
        requirements=_base_requirements(connection, tenant_id, journey_id),
        declaration_rows=_declarations(connection, tenant_id, journey_id),
        audit_documents=_linked_documents(connection, tenant_id, journey_id),
    )


@router.get("/delivery/capture-local", response_model=DeliveryCaptureV2Response)
def get_delivery_capture_local_v2(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeliveryCaptureV2Response:
    state = _authorize_delivery(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _build_local_delivery_capture_response(
        journey_id=journey_id,
        requirements=_delivery_requirements(connection, tenant_id, journey_id),
        audit_documents=_linked_delivery_documents(connection, tenant_id, journey_id),
        submitted=state.get("capture_completed_at_utc") is not None,
    )
