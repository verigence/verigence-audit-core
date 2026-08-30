from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.di_capture_v2_client import DiCaptureV2Client, DiCaptureV2Error
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import ConflictError, DependencyUnavailableError, NotFoundError
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_attribute_mapping import (
    AttributeCandidate,
    comparison_state,
    resolve_candidate,
    spec_for_field,
    specs_for_stage,
)
from audit_core.uc03_attribute_resolution import (
    apply_supported_operational_attribute,
    record_attribute_resolution,
)
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _parse_if_match,
)
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
_RECEIPT_DOCUMENT_TYPE = "dealer_receipt"


class ReviewV2Field(BaseModel):
    canonicalFieldId: str
    fieldKey: str
    value: Any | None = None
    confidenceScore: float | None = None
    sourceFactVersion: int
    reviewState: Literal["READY", "NEEDS_REVIEW"]
    source: Literal["DI"] = "DI"
    pageNo: int | None = None
    evidenceRegion: dict[str, Any] | None = None


class ReviewV2Document(BaseModel):
    documentId: UUID
    evidenceId: UUID | None = None
    requirementKey: str | None = None
    label: str
    documentTypeKey: str | None = None
    originalFilename: str
    contentUrl: str | None = None
    processingStatus: str
    extractionState: Literal["PENDING", "READY", "FAILED"]
    fields: list[ReviewV2Field]


class ReviewV2SourceValue(BaseModel):
    canonicalFieldId: str
    fieldKey: str
    value: Any | None = None
    confidenceScore: float | None = None
    sourceFactVersion: int
    reviewState: Literal["READY", "NEEDS_REVIEW"]
    documentId: UUID
    evidenceId: UUID | None = None
    documentTypeKey: str | None = None
    documentLabel: str
    originalFilename: str
    contentUrl: str | None = None
    pageNo: int | None = None
    evidenceRegion: dict[str, Any] | None = None


class ReviewV2Attribute(BaseModel):
    attributeKey: str
    excelFieldNo: int | None = None
    label: str
    mappingStatus: Literal["SUPPORTED", "PROVISIONAL"]
    operationalField: str | None = None
    resolvedValue: Any | None = None
    confidenceScore: float | None = None
    reviewState: Literal["READY", "NEEDS_REVIEW"]
    comparisonState: Literal["MATCH", "MISMATCH", "SINGLE_SOURCE", "NOT_AVAILABLE"]
    resolvedSource: ReviewV2SourceValue | None = None
    sources: list[ReviewV2SourceValue]


class ReviewV2UnmappedField(BaseModel):
    canonicalFieldId: str
    fieldKey: str
    value: Any | None = None
    confidenceScore: float | None = None
    sourceFactVersion: int
    documentId: UUID
    documentTypeKey: str | None = None
    documentLabel: str
    originalFilename: str
    pageNo: int | None = None
    evidenceRegion: dict[str, Any] | None = None


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
    aggregateVersion: int
    processingPending: bool
    needsReviewCount: int
    attributes: list[ReviewV2Attribute]
    unmappedFields: list[ReviewV2UnmappedField]
    documents: list[ReviewV2Document]
    missingDeclarations: list[ReviewV2MissingDeclaration]


class BookingReviewV2ConfirmResponse(BaseModel):
    journeyId: UUID
    pcVerificationStatus: Literal["VERIFIED"] = "VERIFIED"
    aggregateVersion: int
    resolvedAttributeCount: int
    appliedAttributes: list[str]
    reviewOnlyAttributes: list[str]
    conflictAttributes: list[str]


class AuditSourceComparisonV2Response(BaseModel):
    journeyId: UUID
    deliverySubmitted: bool
    processingPending: bool
    attributes: list[ReviewV2Attribute]
    unmappedFields: list[ReviewV2UnmappedField]
    documents: list[ReviewV2Document]


def _field_review_state(*, value: Any, confidence_score: float | None) -> Literal["READY", "NEEDS_REVIEW"]:
    if value is None or confidence_score is None or confidence_score < _REVIEW_THRESHOLD:
        return "NEEDS_REVIEW"
    return "READY"


def _stage_submission_state(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
) -> tuple[bool, str, int]:
    row = connection.execute(
        text(
            """
            SELECT capture_completed_at_utc, pc_verification_status, version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code=:stage_code
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).mappings().one_or_none()
    submitted = row is not None and row["capture_completed_at_utc"] is not None
    status = "NOT_SUBMITTED"
    version = int(row["version_no"]) if row is not None else 0
    if submitted:
        status = str(row["pc_verification_status"] or "PENDING")
    return submitted, status, version


def _submission_state(connection: Connection, *, tenant_id: str, journey_id: UUID) -> tuple[bool, str, int]:
    return _stage_submission_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="BOOKING",
    )


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


def _legacy_evidence_links(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stages: tuple[str, ...],
) -> list[dict[str, Any]]:
    allowed = {stage.upper() for stage in stages}
    rows = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.di_document_id, e.document_type_key,
                   e.evidence_purpose, jdr.requirement_key, upper(jdr.process_area) AS stage_code
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_requirements jdr
              ON jdr.tenant_id=e.tenant_id
             AND jdr.journey_document_requirement_id=e.journey_document_requirement_id
            WHERE e.tenant_id=:tenant_id
              AND e.journey_id=:journey_id
              AND e.association_status='ACTIVE'
              AND e.di_document_id IS NOT NULL
              AND upper(jdr.process_area) IN ('BOOKING','DELIVERY')
            ORDER BY e.linked_at_utc, e.evidence_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows if str(row["stage_code"]).upper() in allowed]


def _review_document(
    *,
    token: str,
    tenant_id: str,
    context_ref: str,
    di_client: DiClient,
    document_id: UUID,
    label: str,
    original_filename: str,
    document_type_key: str | None,
    requirement_key: str | None,
    content_url: str | None,
    processing_status_hint: str | None,
    evidence_id: UUID | None = None,
) -> ReviewV2Document:
    processing_status = processing_status_hint or "NOT_STARTED"
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
                        canonicalFieldId=fact.canonical_field_id,
                        fieldKey=fact.field_key,
                        value=fact.value,
                        confidenceScore=fact.confidence_score,
                        sourceFactVersion=fact.version_no,
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
        evidenceId=evidence_id,
        requirementKey=requirement_key,
        label=label,
        documentTypeKey=document_type_key,
        originalFilename=original_filename,
        contentUrl=content_url,
        processingStatus=processing_status,
        extractionState=extraction_state,
        fields=fields,
    )


def _source_value(document: ReviewV2Document, field: ReviewV2Field) -> ReviewV2SourceValue:
    return ReviewV2SourceValue(
        canonicalFieldId=field.canonicalFieldId,
        fieldKey=field.fieldKey,
        value=field.value,
        confidenceScore=field.confidenceScore,
        sourceFactVersion=field.sourceFactVersion,
        reviewState=field.reviewState,
        documentId=document.documentId,
        evidenceId=document.evidenceId,
        documentTypeKey=document.documentTypeKey,
        documentLabel=document.label,
        originalFilename=document.originalFilename,
        contentUrl=document.contentUrl,
        pageNo=field.pageNo,
        evidenceRegion=field.evidenceRegion,
    )


def _candidate(document: ReviewV2Document, field: ReviewV2Field) -> AttributeCandidate:
    return AttributeCandidate(
        field_key=field.fieldKey,
        value=field.value,
        confidence_score=field.confidenceScore,
        document_id=str(document.documentId),
        document_type_key=document.documentTypeKey,
        document_label=document.label,
        original_filename=document.originalFilename,
        content_url=document.contentUrl,
        page_no=field.pageNo,
        evidence_region=field.evidenceRegion,
        evidence_id=str(document.evidenceId) if document.evidenceId else None,
        canonical_field_id=field.canonicalFieldId,
        source_fact_version=field.sourceFactVersion,
    )


def _build_attributes(
    documents: list[ReviewV2Document],
    *,
    stages: tuple[str, ...],
) -> tuple[list[ReviewV2Attribute], list[ReviewV2UnmappedField]]:
    grouped: dict[str, list[tuple[ReviewV2Document, ReviewV2Field]]] = {}
    unmapped: list[ReviewV2UnmappedField] = []

    for document in documents:
        receipt_scoped = (
            "BOOKING" in stages
            and str(document.documentTypeKey or "").strip().lower() == _RECEIPT_DOCUMENT_TYPE
        )
        for field in document.fields:
            spec = None if receipt_scoped else spec_for_field(field.fieldKey)
            if spec is None or not any(stage in spec.stages for stage in stages):
                unmapped.append(
                    ReviewV2UnmappedField(
                        canonicalFieldId=field.canonicalFieldId,
                        fieldKey=field.fieldKey,
                        value=field.value,
                        confidenceScore=field.confidenceScore,
                        sourceFactVersion=field.sourceFactVersion,
                        documentId=document.documentId,
                        documentTypeKey=document.documentTypeKey,
                        documentLabel=document.label,
                        originalFilename=document.originalFilename,
                        pageNo=field.pageNo,
                        evidenceRegion=field.evidenceRegion,
                    )
                )
                continue
            grouped.setdefault(spec.attribute_key, []).append((document, field))

    result: list[ReviewV2Attribute] = []
    seen: set[str] = set()
    for stage in stages:
        for spec in specs_for_stage(stage):
            if spec.attribute_key in seen:
                continue
            seen.add(spec.attribute_key)
            pairs = grouped.get(spec.attribute_key, [])
            candidates = [_candidate(document, field) for document, field in pairs]
            resolved = resolve_candidate(spec, candidates)
            resolved_source: ReviewV2SourceValue | None = None
            if resolved is not None:
                for document, field in pairs:
                    if (
                        str(document.documentId) == resolved.document_id
                        and field.fieldKey == resolved.field_key
                        and field.sourceFactVersion == resolved.source_fact_version
                    ):
                        resolved_source = _source_value(document, field)
                        break
            sources = [_source_value(document, field) for document, field in pairs]
            sources.sort(
                key=lambda source: (
                    source.documentLabel.casefold(),
                    source.fieldKey.casefold(),
                    str(source.documentId),
                )
            )
            review_state: Literal["READY", "NEEDS_REVIEW"] = "NEEDS_REVIEW"
            if resolved_source is not None:
                review_state = resolved_source.reviewState
            result.append(
                ReviewV2Attribute(
                    attributeKey=spec.attribute_key,
                    excelFieldNo=spec.excel_field_no,
                    label=spec.label,
                    mappingStatus=spec.mapping_status,
                    operationalField=spec.operational_field,
                    resolvedValue=resolved.value if resolved is not None else None,
                    confidenceScore=(resolved.confidence_score if resolved is not None else None),
                    reviewState=review_state,
                    comparisonState=comparison_state(candidates),
                    resolvedSource=resolved_source,
                    sources=sources,
                )
            )

    result.sort(key=lambda item: (item.excelFieldNo is None, item.excelFieldNo or 9999, item.label))
    unmapped.sort(key=lambda item: (item.documentLabel.casefold(), item.fieldKey.casefold()))
    return result, unmapped


def _v2_documents_for_stage(
    *,
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
    token: str,
    context_ref: str,
    di_client: DiClient,
    v2_client: DiCaptureV2Client,
    stage: str,
    requirements: list[dict[str, Any]] | None = None,
) -> list[ReviewV2Document]:
    try:
        di_payload = v2_client.list_documents(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            phase=stage,
        )
    except DiCaptureV2Error as exc:
        if stage == "DELIVERY" and exc.status_code in {404, 405}:
            return []
        raise DependencyUnavailableError(
            detail="Document review status is temporarily unavailable."
        ) from exc

    di_documents = list(di_payload.get("documents") or [])
    if stage == "BOOKING" and requirements is not None:
        _reconcile_documents(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirements=requirements,
            di_documents=di_documents,
        )
        links = _linked_documents(connection, tenant_id, journey_id)
    else:
        rows = connection.execute(
            text(
                """
                SELECT di_document_id, requirement_key, classified_document_type_key,
                       original_filename, content_type, capture_status
                FROM auditcore.document_capture_v2_documents
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code=:stage AND capture_status <> 'SUPERSEDED'
                ORDER BY created_at_utc, di_document_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "stage": stage},
        ).mappings().all()
        links = [dict(row) for row in rows]

    link_by_id = {str(row["di_document_id"]): row for row in links}
    requirement_by_key = {
        str(row["requirement_key"]): row
        for row in (requirements or [])
        if row.get("requirement_key")
    }
    documents: list[ReviewV2Document] = []
    for di_status in di_documents:
        if str(di_status.get("state") or "").upper() != "CLASSIFIED":
            continue
        document_id = UUID(str(di_status["documentId"]))
        link = link_by_id.get(str(document_id), {})
        requirement_key = str(link["requirement_key"]) if link.get("requirement_key") else None
        requirement = requirement_by_key.get(requirement_key) if requirement_key else None
        label = (
            str(requirement["display_label"])
            if requirement is not None
            else str(di_status.get("classifiedDocumentTypeKey") or requirement_key or "Document")
        )
        try:
            documents.append(
                _review_document(
                    token=token,
                    tenant_id=tenant_id,
                    context_ref=context_ref,
                    di_client=di_client,
                    document_id=document_id,
                    label=label,
                    original_filename=str(di_status.get("originalFilename") or link.get("original_filename") or document_id),
                    document_type_key=(di_status.get("classifiedDocumentTypeKey") or link.get("classified_document_type_key")),
                    requirement_key=requirement_key,
                    content_url=di_status.get("contentUrl"),
                    processing_status_hint=di_status.get("processingStatus"),
                )
            )
        except DiClientError as exc:
            if exc.retryable:
                documents.append(
                    ReviewV2Document(
                        documentId=document_id,
                        requirementKey=requirement_key,
                        label=label,
                        documentTypeKey=(di_status.get("classifiedDocumentTypeKey") or link.get("classified_document_type_key")),
                        originalFilename=str(di_status.get("originalFilename") or link.get("original_filename") or document_id),
                        contentUrl=di_status.get("contentUrl"),
                        processingStatus=str(di_status.get("processingStatus") or "PROCESSING"),
                        extractionState="PENDING",
                        fields=[],
                    )
                )
            else:
                raise DependencyUnavailableError(
                    detail="Document review values are temporarily unavailable."
                ) from exc
    return documents


def _legacy_documents(
    *,
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
    token: str,
    context_ref: str,
    di_client: DiClient,
    stages: tuple[str, ...],
    excluded_document_ids: set[str],
) -> list[ReviewV2Document]:
    documents: list[ReviewV2Document] = []
    for link in _legacy_evidence_links(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stages=stages,
    ):
        document_id = UUID(str(link["di_document_id"]))
        if str(document_id) in excluded_document_ids:
            continue
        document_type = str(link["document_type_key"]) if link.get("document_type_key") else None
        requirement_key = str(link["requirement_key"]) if link.get("requirement_key") else None
        label = requirement_key.replace("_", " ").title() if requirement_key else (document_type or "Document")
        try:
            documents.append(
                _review_document(
                    token=token,
                    tenant_id=tenant_id,
                    context_ref=context_ref,
                    di_client=di_client,
                    document_id=document_id,
                    evidence_id=UUID(str(link["evidence_id"])),
                    label=label,
                    original_filename=f"{label} · {str(document_id)[:8]}",
                    document_type_key=document_type,
                    requirement_key=requirement_key,
                    content_url=None,
                    processing_status_hint=None,
                )
            )
        except DiClientError as exc:
            if not exc.retryable:
                raise DependencyUnavailableError(
                    detail="Document review values are temporarily unavailable."
                ) from exc
    return documents


def _all_review_documents(
    *,
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
    token: str,
    context_ref: str,
    di_client: DiClient,
    v2_client: DiCaptureV2Client,
    stages: tuple[str, ...],
    requirements: list[dict[str, Any]] | None = None,
) -> list[ReviewV2Document]:
    documents: list[ReviewV2Document] = []
    for stage in stages:
        documents.extend(
            _v2_documents_for_stage(
                connection=connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                token=token,
                context_ref=context_ref,
                di_client=di_client,
                v2_client=v2_client,
                stage=stage,
                requirements=requirements if stage == "BOOKING" else None,
            )
        )
    ids = {str(document.documentId) for document in documents}
    documents.extend(
        _legacy_documents(
            connection=connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            token=token,
            context_ref=context_ref,
            di_client=di_client,
            stages=stages,
            excluded_document_ids=ids,
        )
    )
    return documents


def _booking_review_data(
    *,
    connection: Connection,
    engine: Engine,
    tenant_id: str,
    journey_id: UUID,
    security_client: SecurityOAuthClient,
    di_client: DiClient,
    v2_client: DiCaptureV2Client,
) -> tuple[list[dict[str, Any]], list[ReviewV2Document], list[ReviewV2Attribute], list[ReviewV2UnmappedField]]:
    requirements = _base_requirements(connection, tenant_id, journey_id)
    context_ref, token = _ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    documents = _all_review_documents(
        connection=connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        token=token,
        context_ref=context_ref,
        di_client=di_client,
        v2_client=v2_client,
        stages=("BOOKING",),
        requirements=requirements,
    )
    attributes, unmapped = _build_attributes(documents, stages=("BOOKING",))
    return requirements, documents, attributes, unmapped


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
    capture_submitted, verification_status, aggregate_version = _submission_state(
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

    requirements, documents, attributes, unmapped = _booking_review_data(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )
    needs_review = sum(1 for attribute in attributes if attribute.resolvedValue is not None and attribute.reviewState == "NEEDS_REVIEW")
    pending = any(document.extractionState == "PENDING" for document in documents)
    return BookingReviewV2Response(
        journeyId=journey_id,
        captureSubmitted=True,
        pcVerificationStatus=verification_status,
        aggregateVersion=aggregate_version,
        processingPending=pending,
        needsReviewCount=needs_review,
        attributes=attributes,
        unmappedFields=unmapped,
        documents=documents,
        missingDeclarations=_missing_declarations(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirements=requirements,
        ),
    )


@router.post("/booking/review/confirm", response_model=BookingReviewV2ConfirmResponse)
def confirm_booking_review_v2(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> BookingReviewV2ConfirmResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    _, documents, attributes, _ = _booking_review_data(
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
            detail="Document Intelligence is still preparing one or more Booking documents.",
        )
    if any(document.extractionState == "FAILED" for document in documents):
        raise ConflictError(
            error_code="VAC-CONFLICT-011",
            title="Document processing requires follow-up",
            detail="One or more Booking documents failed processing and must be resolved before verification.",
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
                  AND stage_code='BOOKING'
                FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one_or_none()
        if state is None or state["capture_completed_at_utc"] is None:
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Booking capture has not been submitted",
                detail="Submit Booking before completing Review.",
            )
        if int(state["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Booking version conflict",
                detail="Booking changed since Review was loaded. Refresh Review and try again.",
            )
        if str(state["pc_verification_status"] or "PENDING") != "PENDING":
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Booking Review is not pending",
                detail="This Booking Review has already been completed.",
            )

        applied: list[str] = []
        review_only: list[str] = []
        conflicts: list[str] = []
        resolved_count = 0
        for attribute in attributes:
            source = attribute.resolvedSource
            if source is None or attribute.resolvedValue is None:
                continue
            spec = spec_for_field(source.fieldKey)
            if spec is None or spec.attribute_key != attribute.attributeKey:
                continue
            resolved_count += 1
            application = apply_supported_operational_attribute(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                spec=spec,
                value=attribute.resolvedValue,
                actor_id=human_principal.subject,
                source_document_type_key=source.documentTypeKey,
                source_field_key=source.fieldKey,
                source_evidence_id=source.evidenceId,
            )
            owning_domain_key: str | None = None
            owning_record_reference: str | None = None
            if application is None:
                review_only.append(attribute.attributeKey)
            else:
                owning_domain_key, owning_record_reference, application_status = application
                applied.append(attribute.attributeKey)
                if application_status == "CONFLICT":
                    conflicts.append(attribute.attributeKey)

            if spec.mapping_status == "SUPPORTED":
                record_attribute_resolution(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code="BOOKING",
                    spec=spec,
                    source_di_document_id=source.documentId,
                    source_evidence_id=source.evidenceId,
                    source_canonical_field_id=source.canonicalFieldId,
                    source_field_key=source.fieldKey,
                    source_fact_version=source.sourceFactVersion,
                    source_document_type_key=source.documentTypeKey,
                    actor_id=human_principal.subject,
                    owning_domain_key=owning_domain_key,
                    owning_record_reference=owning_record_reference,
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
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=f"{idempotency_key}:review-confirmed",
            correlation_id=correlation_id,
            safe_payload={
                "resolvedAttributeCount": resolved_count,
                "appliedAttributeKeys": sorted(applied),
                "reviewOnlyAttributeKeys": sorted(review_only),
                "conflictAttributeKeys": sorted(conflicts),
                "rawDiValuesCopied": False,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "pcVerificationStatus": "VERIFIED",
            "aggregateVersion": next_version,
            "resolvedAttributeCount": resolved_count,
            "appliedAttributes": sorted(applied),
            "reviewOnlyAttributes": sorted(review_only),
            "conflictAttributes": sorted(conflicts),
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.attribute-review.confirm:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version},
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return BookingReviewV2ConfirmResponse.model_validate(body)


@router.get("/audit/source-comparison", response_model=AuditSourceComparisonV2Response)
def get_audit_source_comparison_v2(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[DiCaptureV2Client, Depends(get_di_capture_v2_client)],
) -> AuditSourceComparisonV2Response:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    delivery_submitted, _, _ = _stage_submission_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="DELIVERY",
    )
    if not delivery_submitted:
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Delivery has not been submitted",
            detail="The cross-source Audit View becomes available after Delivery submission.",
        )

    requirements = _base_requirements(connection, tenant_id, journey_id)
    context_ref, token = _ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    documents = _all_review_documents(
        connection=connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        token=token,
        context_ref=context_ref,
        di_client=di_client,
        v2_client=v2_client,
        stages=("BOOKING", "DELIVERY"),
        requirements=requirements,
    )
    attributes, unmapped = _build_attributes(documents, stages=("BOOKING", "DELIVERY"))
    return AuditSourceComparisonV2Response(
        journeyId=journey_id,
        deliverySubmitted=True,
        processingPending=any(document.extractionState == "PENDING" for document in documents),
        attributes=attributes,
        unmappedFields=unmapped,
        documents=documents,
    )


def _document_is_linked(connection: Connection, *, tenant_id: str, journey_id: UUID, document_id: UUID) -> bool:
    direct = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND di_document_id=:document_id AND capture_status <> 'SUPERSEDED'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document_id},
    ).scalar_one_or_none()
    if direct is not None:
        return True
    legacy = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND di_document_id=:document_id AND association_status='ACTIVE'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document_id},
    ).scalar_one_or_none()
    return legacy is not None


@router.get("/review/documents/{document_id}/content")
def get_review_document_content_v2(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
) -> Response:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    if not _document_is_linked(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        document_id=document_id,
    ):
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Document not found",
            detail="The requested source document is not linked to this Journey.",
        )
    context_ref, token = _ensure_di_context(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
    )
    try:
        content, content_type, content_disposition = di_client.get_audit_document_content(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            document_id=str(document_id),
        )
    except DiClientError as exc:
        raise DependencyUnavailableError(
            detail="Source document content is temporarily unavailable."
        ) from exc
    headers: dict[str, str] = {"Cache-Control": "private, no-store"}
    if content_disposition:
        headers["Content-Disposition"] = content_disposition
    return Response(content=content, media_type=content_type, headers=headers)
