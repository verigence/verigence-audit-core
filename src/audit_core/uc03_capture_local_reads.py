from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_delivery_capture_v2 import (
    DeliveryCaptureV2Response,
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
    # Direct user correction (2026-09-24): _authorize_delivery (via
    # _delivery_state) used to gate this endpoint, and raises VAC-NF-005
    # ("Start Delivery before capturing Delivery documents") whenever
    # journey_stage_states has no DELIVERY row yet -- i.e. for every journey
    # still in Booking, which is the normal, common case for a PC opening
    # the combined checklist. That 404 propagated to the frontend as a
    # silently-swallowed query error (retry: false, no error banner wired
    # to this specific query), so the unified Documents checklist rendered
    # with the Delivery half simply empty and every visible card -- even
    # "Missing" placeholders -- defaulting to a "BOOKING" label with no
    # indication anything had failed. This is a read-only preview endpoint,
    # like booking/capture-local next to it: the active-Delivery-stage guard
    # belongs on capture/extraction mutations, not here, so the PC can see
    # what Delivery will expect before Delivery has actually started.
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    # Delivery's requirement rows are otherwise only seeded lazily -- by the
    # unified upload-intents endpoint, or by Delivery actually starting. A
    # PC opening the combined checklist before uploading anything (the
    # normal Capture New Booking flow) must still see what Delivery will
    # expect, so seed here too. Idempotent, no business-meaningful side
    # effect (see auditcore.seed_delivery_document_requirements).
    connection.execute(
        text("SELECT auditcore.seed_delivery_document_requirements(:tenant_id, :journey_id)"),
        {"tenant_id": tenant_id, "journey_id": str(journey_id)},
    )
    delivery_state = connection.execute(
        text(
            "SELECT capture_completed_at_utc FROM auditcore.journey_stage_states "
            "WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND stage_code='DELIVERY'"
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    return _build_local_delivery_capture_response(
        journey_id=journey_id,
        requirements=_delivery_requirements(connection, tenant_id, journey_id),
        audit_documents=_linked_delivery_documents(connection, tenant_id, journey_id),
        submitted=bool(delivery_state and delivery_state["capture_completed_at_utc"] is not None),
    )
