from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, Header, Request, Response
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core import uc03_document_review_v2 as review_v2
from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.errors import ConflictError
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_booking_commands import _aggregate_lock, _parse_if_match
from audit_core.uc03_delivery_commands import _append_delivery_event
from audit_core.uc03_di_core_persistence import ReviewedDiField, persist_reviewed_di_fields


class DeliveryReviewV2ConfirmResponse(BaseModel):
    journeyId: UUID
    pcVerificationStatus: Literal["VERIFIED"] = "VERIFIED"
    aggregateVersion: int
    storedFieldCount: int


def _lossless_delivery_fields(
    documents: list[review_v2.ReviewV2Document],
) -> list[ReviewedDiField]:
    reviewed: list[ReviewedDiField] = []
    for document in documents:
        for field in document.fields:
            reviewed.append(
                ReviewedDiField(
                    document_id=document.documentId,
                    evidence_id=document.evidenceId,
                    source_canonical_field_id=field.canonicalFieldId,
                    source_document_type_key=document.documentTypeKey,
                    field_key=field.fieldKey,
                    source_fact_version=field.sourceFactVersion,
                    extracted_value=field.value,
                    effective_value=field.value,
                    confidence_score=field.confidenceScore,
                    confidence_scale=(
                        "PERCENT" if field.confidenceScore is not None else None
                    ),
                    is_modified=False,
                )
            )
    return reviewed


def _delivery_review_documents(
    *,
    connection: Connection,
    engine: Engine,
    tenant_id: str,
    journey_id: UUID,
    security_client: SecurityOAuthClient,
    di_client: review_v2.DiClient,
    v2_client: review_v2.DiCaptureV2Client,
) -> list[review_v2.ReviewV2Document]:
    context_ref, token = review_v2._ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    return review_v2._all_review_documents(
        connection=connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        token=token,
        context_ref=context_ref,
        di_client=di_client,
        v2_client=v2_client,
        stages=("DELIVERY",),
    )


def confirm_delivery_review_v2(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
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
) -> DeliveryReviewV2ConfirmResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    submitted, verification_status, _ = review_v2._stage_submission_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="DELIVERY",
    )
    if not submitted:
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Delivery has not been submitted",
            detail="Submit Delivery document capture before completing Delivery Review.",
        )
    if verification_status != "PENDING":
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Delivery Review is not pending",
            detail="This Delivery Review has already been completed.",
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
    if any(document.extractionState == "PENDING" for document in documents):
        raise ConflictError(
            error_code="VAC-CONFLICT-011",
            title="Documents are not ready for review",
            detail="Document Intelligence is still preparing one or more Delivery documents.",
        )
    if any(document.extractionState == "FAILED" for document in documents):
        raise ConflictError(
            error_code="VAC-CONFLICT-011",
            title="Document processing requires follow-up",
            detail="One or more Delivery documents failed processing and require follow-up.",
        )

    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = connection.execute(
            text(
                """
                SELECT capture_completed_at_utc, pc_verification_status, version_no
                FROM auditcore.journey_stage_states
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='DELIVERY'
                FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one_or_none()
        if state is None or state["capture_completed_at_utc"] is None:
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Delivery has not been submitted",
                detail="Submit Delivery document capture before completing Delivery Review.",
            )
        if int(state["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Delivery version conflict",
                detail="Delivery changed since Review was loaded. Refresh Review and try again.",
            )
        if str(state["pc_verification_status"] or "PENDING") != "PENDING":
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Delivery Review is not pending",
                detail="This Delivery Review has already been completed.",
            )

        stored_field_count = persist_reviewed_di_fields(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="DELIVERY",
            actor_id=human_principal.subject,
            fields=_lossless_delivery_fields(documents),
        )
        next_version = expected_version + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET pc_verification_status='VERIFIED',
                    latest_activity_at_utc=now(),
                    updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='DELIVERY'
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "version": next_version,
            },
        )
        _append_delivery_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_DELIVERY_REVIEW_CONFIRMED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=f"{idempotency_key}:review-confirmed",
            correlation_id=correlation_id,
            safe_payload={
                "storedFieldCount": stored_field_count,
                "rawDiValuesCopied": True,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "pcVerificationStatus": "VERIFIED",
            "aggregateVersion": next_version,
            "storedFieldCount": stored_field_count,
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.delivery.review.confirm:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version},
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return DeliveryReviewV2ConfirmResponse.model_validate(body)


def install_uc03_delivery_review_confirm() -> None:
    if getattr(review_v2, "_delivery_review_confirm_installed", False):
        return
    review_v2.router.add_api_route(
        "/delivery/review/confirm",
        confirm_delivery_review_v2,
        methods=["POST"],
        response_model=DeliveryReviewV2ConfirmResponse,
    )
    review_v2._delivery_review_confirm_installed = True
