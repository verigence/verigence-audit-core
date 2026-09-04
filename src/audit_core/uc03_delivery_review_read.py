from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy import Connection, Engine

from audit_core import uc03_document_review_v2 as review_v2
from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.errors import ConflictError
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_delivery_review_confirm import _delivery_review_documents


class DeliveryReviewV2Response(BaseModel):
    journeyId: UUID
    phase: str = "DELIVERY"
    captureSubmitted: bool
    pcVerificationStatus: str
    aggregateVersion: int
    processingPending: bool
    needsReviewCount: int
    attributes: list[review_v2.ReviewV2Attribute]
    unmappedFields: list[review_v2.ReviewV2UnmappedField]
    documents: list[review_v2.ReviewV2Document]


def get_delivery_review_v2(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ],
    di_client: Annotated[review_v2.DiClient, Depends(get_di_client)],
    v2_client: Annotated[
        review_v2.DiCaptureV2Client,
        Depends(review_v2.get_di_capture_v2_client),
    ],
) -> DeliveryReviewV2Response:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    submitted, verification_status, aggregate_version = review_v2._stage_submission_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="DELIVERY",
    )
    if not submitted:
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Delivery has not been submitted",
            detail="Submit Delivery document capture before opening Delivery Review.",
        )

    documents = _delivery_review_documents(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )
    attributes, unmapped = review_v2._build_attributes(documents, stages=("DELIVERY",))
    pending = any(document.extractionState == "PENDING" for document in documents)
    needs_review = sum(
        1
        for attribute in attributes
        if attribute.resolvedValue is not None
        and attribute.reviewState == "NEEDS_REVIEW"
    )
    return DeliveryReviewV2Response(
        journeyId=journey_id,
        captureSubmitted=True,
        pcVerificationStatus=verification_status,
        aggregateVersion=aggregate_version,
        processingPending=pending,
        needsReviewCount=needs_review,
        attributes=attributes,
        unmappedFields=unmapped,
        documents=documents,
    )


def install_uc03_delivery_review_read() -> None:
    if getattr(review_v2, "_delivery_review_read_installed", False):
        return
    review_v2.router.add_api_route(
        "/delivery/review",
        get_delivery_review_v2,
        methods=["GET"],
        response_model=DeliveryReviewV2Response,
    )
    review_v2._delivery_review_read_installed = True
