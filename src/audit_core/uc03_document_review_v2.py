from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.di_capture_v2_client import DiCaptureV2Client, DiCaptureV2Error
from audit_core.di_client import DiClient, DiClientError
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.errors import ConflictError, DependencyUnavailableError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_document_capture_v2 import (
    _base_requirements,
    _declarations,
    _ensure_di_context,
    _linked_documents,
    _reconcile_documents,
    get_di_capture_v2_client,
)

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}",
    tags=["uc03-document-review-v2"],
)

_REVIEW_THRESHOLD = 92.0
_FAILED_PROCESSING = {"FAILED", "ERROR", "REJECTED"}


class ReviewV2Field(BaseModel):
    fieldKey: str
    value: Any | None = None
    reviewState: Literal["READY", "NEEDS_REVIEW"]
    source: Literal["DI"] = "DI"
    pageNo: int | None = None
    evidenceRegion: dict[str, Any] | None = None


class ReviewV2Document(BaseModel):
    documentId: UUID
    requirementKey: str | None = None
    label: str
    documentTypeKey: str | None = None
    originalFilename: str
    contentUrl: str | None = None
    processingStatus: str
    extractionState: Literal["PENDING", "READY", "FAILED"]
    fields: list[ReviewV2Field]


class ReviewV2MissingDeclaration(BaseModel):
    conditionKey: str
    requirementKey: str
    label: str
    applicable: bool
    documentAvailable: bool | None = None


class BookingReviewV2Response(BaseModel):
    journeyId: UUID
    phase: Literal["BOOKING"] = "BOOKING"
    captureSubmitted: bool
    pcVerificationStatus: str
    processingPending: bool
    needsReviewCount: int
    documents: list[ReviewV2Document]
    missingDeclarations: list[ReviewV2MissingDeclaration]


def _field_review_state(*, value: Any, confidence_score: float | None) -> Literal["READY", "NEEDS_REVIEW"]:
    if value is None or confidence_score is None or confidence_score < _REVIEW_THRESHOLD:
        return "NEEDS_REVIEW"
    return "READY"


def _submission_state(connection: Connection, *, tenant_id: str, journey_id: UUID) -> tuple[bool, str]:
    row = connection.execute(
        text(
            """
            SELECT capture_completed_at_utc, pc_verification_status
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    submitted = row is not None and row["capture_completed_at_utc"] is not None
    status = "NOT_SUBMITTED"
    if submitted:
        status = str(row["pc_verification_status"] or "PENDING")
    return submitted, status


def _missing_declarations(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirements: list[dict[str, Any]],
) -> list[ReviewV2MissingDeclaration]:
    declarations = _declarations(connection, tenant_id, journey_id)
    result: list[ReviewV2MissingDeclaration] = []
    for requirement in requirements:
        condition_key = requirement.get("condition_key")
        if not condition_key:
            continue
        declaration = declarations.get(str(condition_key))
        if not declaration:
            continue
        if not bool(declaration["applicable"]) or declaration["document_available"] is not False:
            continue
        result.append(
            ReviewV2MissingDeclaration(
                conditionKey=str(condition_key),
                requirementKey=str(requirement["requirement_key"]),
                label=str(requirement["display_label"]),
                applicable=True,
                documentAvailable=False,
            )
        )
    return result


def _review_document(
    *,
    token: str,
    tenant_id: str,
    context_ref: str,
    di_client: DiClient,
    di_status: dict[str, Any],
    link: dict[str, Any],
    label: str,
) -> ReviewV2Document:
    document_id = UUID(str(di_status["documentId"]))
    processing_status = str(di_status.get("processingStatus") or "NOT_STARTED")
    extraction_state: Literal["PENDING", "READY", "FAILED"] = "PENDING"
    fields: list[ReviewV2Field] = []

    try:
        document = di_client.get_audit_document(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            document_id=str(document_id),
        )
    except DiClientError as exc:
        if not exc.retryable:
            raise
    else:
        processing_status = document.processing_status or processing_status
        normalized_processing = processing_status.upper()
        if normalized_processing in _FAILED_PROCESSING or document.confirmation_status == "NOT_CONFIRMED":
            extraction_state = "FAILED"
        elif document.confirmation_status == "CONFIRMED":
            try:
                facts = di_client.get_audit_document_facts(
                    token=token,
                    tenant_id=tenant_id,
                    external_context_ref=context_ref,
                    document_id=str(document_id),
                )
            except DiClientError as exc:
                if exc.retryable:
                    facts = ()
                else:
                    raise
            else:
                extraction_state = "READY"
            if extraction_state == "READY":
                fields = [
                    ReviewV2Field(
                        fieldKey=fact.field_key,
                        value=fact.value,
                        reviewState=_field_review_state(
                            value=fact.value,
                            confidence_score=fact.confidence_score,
                        ),
                        pageNo=fact.page_no,
                        evidenceRegion=fact.evidence_region,
                    )
                    for fact in facts
                ]

    return ReviewV2Document(
        documentId=document_id,
        requirementKey=(str(link["requirement_key"]) if link.get("requirement_key") else None),
        label=label,
        documentTypeKey=di_status.get("classifiedDocumentTypeKey"),
        originalFilename=str(di_status["originalFilename"]),
        contentUrl=di_status.get("contentUrl"),
        processingStatus=processing_status,
        extractionState=extraction_state,
        fields=fields,
    )


@router.get("/booking/review", response_model=BookingReviewV2Response)
def get_booking_review_v2(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> BookingReviewV2Response:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    capture_submitted, verification_status = _submission_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    if not capture_submitted:
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Booking capture has not been submitted",
            detail="Submit Booking Details before opening Review.",
        )

    requirements = _base_requirements(connection, tenant_id, journey_id)
    requirement_by_key = {str(row["requirement_key"]): row for row in requirements}
    context_ref, token = _ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    try:
        di_payload = v2_client.list_documents(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            phase="BOOKING",
        )
    except DiCaptureV2Error as exc:
        raise DependencyUnavailableError(
            detail="Document review status is temporarily unavailable."
        ) from exc

    di_documents = list(di_payload.get("documents") or [])
    _reconcile_documents(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirements=requirements,
        di_documents=di_documents,
    )
    links = _linked_documents(connection, tenant_id, journey_id)
    link_by_id = {str(row["di_document_id"]): row for row in links}

    documents: list[ReviewV2Document] = []
    for di_status in di_documents:
        if str(di_status.get("state") or "").upper() != "CLASSIFIED":
            continue
        link = link_by_id.get(str(di_status["documentId"]), {})
        requirement_key = str(link["requirement_key"]) if link.get("requirement_key") else None
        requirement = requirement_by_key.get(requirement_key) if requirement_key else None
        label = (
            str(requirement["display_label"])
            if requirement is not None
            else str(di_status.get("classifiedDocumentTypeKey") or "Document")
        )
        try:
            review_document = _review_document(
                token=token,
                tenant_id=tenant_id,
                context_ref=context_ref,
                di_client=di_client,
                di_status=di_status,
                link=link,
                label=label,
            )
        except DiClientError as exc:
            if exc.retryable:
                review_document = ReviewV2Document(
                    documentId=UUID(str(di_status["documentId"])),
                    requirementKey=requirement_key,
                    label=label,
                    documentTypeKey=di_status.get("classifiedDocumentTypeKey"),
                    originalFilename=str(di_status["originalFilename"]),
                    contentUrl=di_status.get("contentUrl"),
                    processingStatus=str(di_status.get("processingStatus") or "PROCESSING"),
                    extractionState="PENDING",
                    fields=[],
                )
            else:
                raise DependencyUnavailableError(
                    detail="Document review values are temporarily unavailable."
                ) from exc
        documents.append(review_document)

    needs_review = sum(
        1
        for document in documents
        for field in document.fields
        if field.reviewState == "NEEDS_REVIEW"
    )
    pending = any(document.extractionState == "PENDING" for document in documents)
    return BookingReviewV2Response(
        journeyId=journey_id,
        captureSubmitted=True,
        pcVerificationStatus=verification_status,
        processingPending=pending,
        needsReviewCount=needs_review,
        documents=documents,
        missingDeclarations=_missing_declarations(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirements=requirements,
        ),
    )
