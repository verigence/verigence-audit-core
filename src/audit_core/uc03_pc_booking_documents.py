from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    get_bearer_token,
    get_connection,
    get_engine,
    get_human_principal,
)
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import (
    AuditCoreError,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
)
from audit_core.evidence import (
    _external_context_ref,
    _journey_context as evidence_journey_context,
    _persist_subject_mapping,
    _subject_mapping,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import (
    HumanPrincipal,
    SecurityTokenError,
    SecurityTokenValidator,
    ServiceIntegrationPrincipal,
)
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError as OAuthTokenError
from audit_core.uc03_booking_capture import (
    _PROPOSAL_CAPTURE_MAP,
    _SUPPORTED_PROPOSAL_FIELDS,
    _require_active_booking,
    _scope,
    _write_typed_capture,
)
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _stage_state,
)
from audit_core.uc03_booking_receipt_capture import (
    _RECEIPT_CAPTURE_MAP,
    _RECEIPT_DOCUMENT_TYPE,
    _write_receipt_capture,
)
from audit_core.uc03_document_assessments import _effective_applicability

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["uc03-pc-booking-documents"])

_DI_AUDIENCE = "di"
_AUDIT_SERVICE_AUDIENCE = "audit"


class BookingUploadRequirement(BaseModel):
    requirementRef: UUID
    requirementKey: str
    documentTypeKey: str
    requirementLevel: str
    requirementStatus: str
    applicabilityState: Literal["APPLICABLE", "NOT_APPLICABLE", "UNRESOLVED"]
    applicabilityReason: str | None = None
    currentDocumentId: UUID | None = None
    captureEligibleFieldKeys: list[str] = Field(default_factory=list)


class BookingUploadContextResponse(BaseModel):
    journeyId: UUID
    externalContextRef: str
    requirements: list[BookingUploadRequirement]


class BookingDocumentLinkCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirementRef: UUID
    documentId: UUID


class BookingDocumentLinkResponse(BaseModel):
    requirementRef: UUID
    documentId: UUID
    evidenceId: UUID
    status: Literal["ACKNOWLEDGED"] = "ACKNOWLEDGED"


class BookingExtractionFieldDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fieldKey: str = Field(min_length=1, max_length=160)
    sourceFactRef: UUID
    sourceFactVersion: Literal[1]
    sourceConfidence: float | None = Field(default=None, ge=0.0, le=100.0)
    decision: Literal["APPROVED", "CORRECTED"]
    approvedValue: Any


class BookingExtractionDecisionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirementRef: UUID
    documentId: UUID
    fields: list[BookingExtractionFieldDecision] = Field(min_length=1, max_length=100)


class BookingExtractionDecisionResult(BaseModel):
    fieldKey: str
    decision: Literal["APPROVED", "CORRECTED"]
    owningDomainKey: str
    owningRecordReference: str
    eventId: UUID


class BookingExtractionDecisionResponse(BaseModel):
    journeyId: UUID
    requirementRef: UUID
    documentId: UUID
    aggregateVersion: int
    decisions: list[BookingExtractionDecisionResult]


@lru_cache
def _audit_service_token_validator() -> SecurityTokenValidator:
    jwks_url = os.environ.get("SECURITY_JWKS_URL", "").strip()
    issuer = os.environ.get("SECURITY_ISSUER", "").strip()
    if not jwks_url or not issuer:
        raise RuntimeError("Security ServiceIntegration verification is not configured")
    return SecurityTokenValidator(
        jwks_url=jwks_url,
        issuer=issuer,
        audience=_AUDIT_SERVICE_AUDIENCE,
    )


def require_audit_service_principal(
    bearer_token: Annotated[str, Depends(get_bearer_token)],
) -> ServiceIntegrationPrincipal:
    try:
        return _audit_service_token_validator().validate_service_integration(bearer_token)
    except SecurityTokenError as exc:
        logger.warning("audit_service_auth_failed", reason=str(exc))
        raise


def _prepare_dependency_error(exc: Exception) -> AuditCoreError:
    if isinstance(exc, DiClientError) and 400 <= exc.status_code < 500:
        return AuditCoreError(
            error_code="VAC-DI-002",
            status_code=422,
            title="Document intelligence rejected Booking context",
            detail="The Booking document upload context could not be prepared in Document Intelligence.",
        )
    return DependencyUnavailableError(
        detail="Booking document preparation is temporarily unavailable. Please try again."
    )


def _applicability(requirement: dict[str, Any]) -> tuple[str, str | None]:
    assessment_state = requirement.get("assessment_applicability_state")
    if assessment_state in {"APPLICABLE", "NOT_APPLICABLE"}:
        reason = requirement.get("assessment_applicability_reason")
        return assessment_state, reason if isinstance(reason, str) else None
    return _effective_applicability(requirement)


def _capture_eligible_field_keys(document_type_key: str) -> list[str]:
    normalized = document_type_key.strip().lower()
    if normalized == _RECEIPT_DOCUMENT_TYPE:
        return sorted(_RECEIPT_CAPTURE_MAP)
    supported = _SUPPORTED_PROPOSAL_FIELDS.get(normalized, set())
    return sorted(field for field in supported if field in _PROPOSAL_CAPTURE_MAP)


@router.post(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/booking/document-upload-context",
    response_model=BookingUploadContextResponse,
)
def prepare_booking_document_upload_context(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
) -> BookingUploadContextResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    _require_active_booking(state)
    journey = evidence_journey_context(connection, tenant_id, journey_id)
    customer_id: UUID = journey["customer_id"]
    context_ref = _external_context_ref(journey_id=journey_id, customer_id=customer_id)

    subject_id = _subject_mapping(
        connection,
        tenant_id=tenant_id,
        customer_id=customer_id,
    )
    try:
        service_token = security_client.get_service_token(audience=_DI_AUDIENCE)
        if subject_id is None:
            subject = di_client.create_subject(
                token=service_token,
                tenant_id=tenant_id,
                subject_type="OTHER",
                display_name=journey["customer_name"],
            )
            subject_id = UUID(subject.subject_id)
            # Persist immediately so a later DI-context failure cannot create a second
            # DI Subject on retry.
            _persist_subject_mapping(
                engine,
                tenant_id=tenant_id,
                customer_id=customer_id,
                subject_id=subject_id,
            )

        di_client.ensure_audit_storage_context(
            token=service_token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            subject_id=str(subject_id),
            dealer_id=str(journey["dealer_id"]),
            outlet_id=str(journey["outlet_id"]),
            customer_id=str(customer_id),
            project_name=journey["project_name"],
            dealer_name=journey["dealer_name"],
            outlet_name=journey["outlet_name"],
            customer_name=journey["customer_name"],
            idempotency_key=f"uc03-pc-booking-context:{journey_id}",
        )
    except (DiClientError, OAuthTokenError, ValueError) as exc:
        raise _prepare_dependency_error(exc) from exc

    rows = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id,
                   jdr.requirement_key,
                   jdr.document_type_key,
                   jdr.requirement_level,
                   jdr.requirement_status,
                   jdr.condition_snapshot,
                   jda.applicability_state AS assessment_applicability_state,
                   jda.applicability_reason AS assessment_applicability_reason,
                   e.di_document_id AS current_di_document_id,
                   COALESCE(dri.sort_order, 999999) AS sort_order
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN auditcore.journey_document_assessments jda
              ON jda.tenant_id=jdr.tenant_id
             AND jda.journey_id=jdr.journey_id
             AND jda.stage_code='BOOKING'
             AND jda.requirement_key=jdr.requirement_key
            LEFT JOIN auditcore.evidence e
              ON e.tenant_id=jda.tenant_id
             AND e.evidence_id=jda.evidence_id
             AND e.association_status='ACTIVE'
            LEFT JOIN auditcore.document_requirement_items dri
              ON dri.tenant_id=jdr.tenant_id
             AND dri.document_requirement_item_id=jdr.document_requirement_item_id
            WHERE jdr.tenant_id=:tenant_id
              AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='BOOKING'
            ORDER BY COALESCE(dri.sort_order, 999999), jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    requirements: list[BookingUploadRequirement] = []
    for row in rows:
        item = dict(row)
        applicability_state, applicability_reason = _applicability(item)
        if applicability_state != "APPLICABLE":
            continue
        requirements.append(
            BookingUploadRequirement(
                requirementRef=item["journey_document_requirement_id"],
                requirementKey=item["requirement_key"],
                documentTypeKey=item["document_type_key"],
                requirementLevel=item["requirement_level"],
                requirementStatus=item["requirement_status"],
                applicabilityState="APPLICABLE",
                applicabilityReason=applicability_reason,
                currentDocumentId=item["current_di_document_id"],
                captureEligibleFieldKeys=_capture_eligible_field_keys(item["document_type_key"]),
            )
        )

    return BookingUploadContextResponse(
        journeyId=journey_id,
        externalContextRef=context_ref,
        requirements=requirements,
    )


def _discover_requirement_for_callback(
    connection: Connection,
    *,
    service_id: str,
    requirement_ref: UUID,
):
    connection.execute(
        text(
            """
            SELECT set_config('app.internal_service_id', :service_id, true),
                   set_config('app.di_requirement_ref', :requirement_ref, true)
            """
        ),
        {"service_id": service_id, "requirement_ref": str(requirement_ref)},
    )
    row = connection.execute(
        text(
            """
            SELECT tenant_id, journey_id, journey_document_requirement_id,
                   requirement_key, document_type_key, requirement_level,
                   requirement_status, condition_snapshot
            FROM auditcore.journey_document_requirements
            WHERE journey_document_requirement_id=:requirement_ref
              AND upper(process_area)='BOOKING'
            """
        ),
        {"requirement_ref": requirement_ref},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Booking document requirement not found",
            detail="The supplied requirementRef is not an active Booking document requirement.",
        )
    return row


def _require_callback_applicable(requirement) -> tuple[str, str | None]:
    state, reason = _effective_applicability(requirement)
    if state != "APPLICABLE":
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking document requirement is not applicable",
            detail="The supplied requirementRef is not currently applicable to this Booking.",
        )
    return state, reason


@router.post(
    "/v1/internal/di/booking-document-links",
    response_model=BookingDocumentLinkResponse,
)
def acknowledge_booking_document_link(
    payload: BookingDocumentLinkCommand,
    service_principal: Annotated[
        ServiceIntegrationPrincipal,
        Depends(require_audit_service_principal),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingDocumentLinkResponse:
    discovered = _discover_requirement_for_callback(
        connection,
        service_id=service_principal.subject,
        requirement_ref=payload.requirementRef,
    )
    tenant_id = str(discovered["tenant_id"])
    journey_id: UUID = discovered["journey_id"]
    set_tenant_context(connection, tenant_id)

    requirement = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id, jdr.requirement_key,
                   jdr.document_type_key, jdr.requirement_level,
                   jdr.requirement_status, jdr.condition_snapshot,
                   j.customer_id, j.document_requirement_profile_version_id
            FROM auditcore.journey_document_requirements jdr
            JOIN auditcore.journeys j
              ON j.tenant_id=jdr.tenant_id AND j.journey_id=jdr.journey_id
            WHERE jdr.tenant_id=:tenant_id
              AND jdr.journey_id=:journey_id
              AND jdr.journey_document_requirement_id=:requirement_ref
              AND upper(jdr.process_area)='BOOKING'
            FOR UPDATE OF jdr
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "requirement_ref": payload.requirementRef,
        },
    ).mappings().one()
    applicability_state, applicability_reason = _require_callback_applicable(requirement)
    customer_id: UUID = requirement["customer_id"]

    subject_id = _subject_mapping(
        connection,
        tenant_id=tenant_id,
        customer_id=customer_id,
    )
    if subject_id is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="DI Subject mapping is not ready",
            detail="Prepare the Booking document upload context before linking a DI document.",
        )

    existing_for_document = connection.execute(
        text(
            """
            SELECT evidence_id, journey_id, journey_document_requirement_id,
                   association_status
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND di_document_id=:document_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "document_id": payload.documentId},
    ).mappings().one_or_none()
    if existing_for_document is not None:
        if (
            existing_for_document["journey_id"] != journey_id
            or existing_for_document["journey_document_requirement_id"] != payload.requirementRef
        ):
            raise ConflictError(
                error_code="VAC-CONFLICT-009",
                title="DI document linkage conflict",
                detail="The DI document is already linked to a different Booking requirement.",
            )
        evidence_id: UUID = existing_for_document["evidence_id"]
        if existing_for_document["association_status"] != "ACTIVE":
            connection.execute(
                text(
                    """
                    UPDATE auditcore.evidence
                    SET association_status='ACTIVE', void_reason=NULL,
                        voided_by_actor_id=NULL, voided_at_utc=NULL
                    WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
                    """
                ),
                {"tenant_id": tenant_id, "evidence_id": evidence_id},
            )
    else:
        prior_evidence_id = connection.execute(
            text(
                """
                SELECT evidence_id
                FROM auditcore.evidence
                WHERE tenant_id=:tenant_id
                  AND journey_id=:journey_id
                  AND journey_document_requirement_id=:requirement_ref
                  AND association_status='ACTIVE'
                ORDER BY linked_at_utc DESC, evidence_id DESC
                LIMIT 1
                FOR UPDATE
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "requirement_ref": payload.requirementRef,
            },
        ).scalar_one_or_none()
        if prior_evidence_id is not None:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.evidence
                    SET association_status='SUPERSEDED'
                    WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
                    """
                ),
                {"tenant_id": tenant_id, "evidence_id": prior_evidence_id},
            )

        evidence_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence (
                    tenant_id, journey_id, customer_id,
                    journey_document_requirement_id,
                    di_subject_id, di_document_id,
                    document_type_key, evidence_purpose, process_area,
                    association_status, supersedes_evidence_id,
                    linked_by_actor_id, correlation_id
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id,
                    :requirement_ref,
                    :subject_id, :document_id,
                    :document_type_key, 'BOOKING_DOCUMENT', 'BOOKING',
                    'ACTIVE', :supersedes_evidence_id,
                    :service_id, NULL
                )
                RETURNING evidence_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "customer_id": customer_id,
                "requirement_ref": payload.requirementRef,
                "subject_id": subject_id,
                "document_id": payload.documentId,
                "document_type_key": requirement["document_type_key"],
                "supersedes_evidence_id": prior_evidence_id,
                "service_id": service_principal.subject,
            },
        ).scalar_one()

    # Make the acknowledged document the current evidence for the requirement. Do
    # not touch answer/remarks when an assessment already exists.
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_assessments (
                tenant_id, journey_id, stage_code,
                journey_document_requirement_id, requirement_key,
                document_requirement_profile_version_id,
                applicability_state, applicability_reason,
                evidence_id
            ) VALUES (
                :tenant_id, :journey_id, 'BOOKING',
                :requirement_ref, :requirement_key,
                :profile_version_id,
                :applicability_state, :applicability_reason,
                :evidence_id
            )
            ON CONFLICT (tenant_id, journey_id, stage_code, requirement_key)
            DO UPDATE SET
                evidence_id=EXCLUDED.evidence_id,
                version_no=auditcore.journey_document_assessments.version_no+1,
                updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "requirement_ref": payload.requirementRef,
            "requirement_key": requirement["requirement_key"],
            "profile_version_id": requirement["document_requirement_profile_version_id"],
            "applicability_state": applicability_state,
            "applicability_reason": applicability_reason,
            "evidence_id": evidence_id,
        },
    )

    return BookingDocumentLinkResponse(
        requirementRef=payload.requirementRef,
        documentId=payload.documentId,
        evidenceId=evidence_id,
    )


def _current_linked_evidence(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirement_ref: UUID,
    document_id: UUID,
):
    row = connection.execute(
        text(
            """
            SELECT jdr.requirement_key, jdr.document_type_key,
                   jda.evidence_id, e.di_document_id
            FROM auditcore.journey_document_requirements jdr
            JOIN auditcore.journey_document_assessments jda
              ON jda.tenant_id=jdr.tenant_id
             AND jda.journey_id=jdr.journey_id
             AND jda.stage_code='BOOKING'
             AND jda.requirement_key=jdr.requirement_key
            JOIN auditcore.evidence e
              ON e.tenant_id=jda.tenant_id
             AND e.evidence_id=jda.evidence_id
             AND e.association_status='ACTIVE'
            WHERE jdr.tenant_id=:tenant_id
              AND jdr.journey_id=:journey_id
              AND jdr.journey_document_requirement_id=:requirement_ref
              AND upper(jdr.process_area)='BOOKING'
            FOR UPDATE OF jdr, jda, e
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "requirement_ref": requirement_ref,
        },
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Booking document requirement not found",
            detail="The Booking document requirement is not linked to current evidence.",
        )
    if row["di_document_id"] != document_id:
        raise ConflictError(
            error_code="VAC-CONFLICT-009",
            title="Booking document changed",
            detail="The reviewed DI document is no longer the current document for this requirement.",
        )
    return row


def _validate_unique_decisions(fields: list[BookingExtractionFieldDecision]) -> None:
    field_keys: set[str] = set()
    fact_pairs: set[tuple[str, UUID]] = set()
    for field in fields:
        normalized_field = field.fieldKey.strip().lower()
        if normalized_field in field_keys:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Duplicate extraction decision",
                detail="Each extracted field may be decided only once in a document batch.",
            )
        field_keys.add(normalized_field)
        pair = (normalized_field, field.sourceFactRef)
        if pair in fact_pairs:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Duplicate extraction source fact",
                detail="Each field/source fact pair may be submitted only once.",
            )
        fact_pairs.add(pair)


@router.post(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/booking/document-extraction-decisions",
    response_model=BookingExtractionDecisionResponse,
)
def submit_booking_document_extraction_decisions(
    tenant_id: str,
    journey_id: UUID,
    payload: BookingExtractionDecisionCommand,
    request: Request,
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
) -> BookingExtractionDecisionResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    correlation_id = get_correlation_id(request)
    _validate_unique_decisions(payload.fields)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_active_booking(state)
        linked = _current_linked_evidence(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirement_ref=payload.requirementRef,
            document_id=payload.documentId,
        )
        document_type_key = str(linked["document_type_key"] or "").strip().lower()
        allowed_source_fields = (
            set(_RECEIPT_CAPTURE_MAP)
            if document_type_key == _RECEIPT_DOCUMENT_TYPE
            else _SUPPORTED_PROPOSAL_FIELDS.get(document_type_key, set())
        )
        evidence_id: UUID = linked["evidence_id"]
        next_version = int(state["version_no"]) + 1
        results: list[dict[str, Any]] = []

        for index, field in enumerate(payload.fields):
            source_field_key = field.fieldKey.strip().lower()
            receipt_capture_key = (
                _RECEIPT_CAPTURE_MAP.get(source_field_key)
                if document_type_key == _RECEIPT_DOCUMENT_TYPE
                else None
            )
            normal_capture_key = _PROPOSAL_CAPTURE_MAP.get(source_field_key)
            capture_key = receipt_capture_key or normal_capture_key
            if capture_key is None or source_field_key not in allowed_source_fields:
                raise AuditCoreError(
                    error_code="VAC-VAL-002",
                    status_code=422,
                    title="Unsupported extraction field",
                    detail="This DI field does not have an approved Booking typed-domain mapping.",
                )
            if receipt_capture_key is not None:
                domain, record_reference = _write_receipt_capture(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    capture_key=receipt_capture_key,
                    value=field.approvedValue,
                    source_evidence_id=evidence_id,
                )
            else:
                domain, record_reference = _write_typed_capture(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    field_key=capture_key,
                    value=field.approvedValue,
                    source_evidence_id=evidence_id,
                )
            event_id = _append_workflow_event(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                event_type=(
                    "BOOKING_EXTRACTION_APPROVED"
                    if field.decision == "APPROVED"
                    else "BOOKING_EXTRACTION_CORRECTED"
                ),
                source_kind="HUMAN",
                actor_id=human_principal.subject,
                actor_role_snapshot=context["operating_role"],
                idempotency_key=f"{idempotency_key}:{index}",
                correlation_id=correlation_id,
                safe_payload={
                    "requirementRef": str(payload.requirementRef),
                    "documentId": str(payload.documentId),
                    "fieldKey": source_field_key,
                    "sourceFactRef": str(field.sourceFactRef),
                    "sourceFactVersion": field.sourceFactVersion,
                    "sourceConfidence": field.sourceConfidence,
                    "decision": field.decision,
                    "owningDomainKey": domain,
                    "owningRecordReference": record_reference,
                },
                aggregate_version=next_version,
            )
            results.append(
                {
                    "fieldKey": source_field_key,
                    "decision": field.decision,
                    "owningDomainKey": domain,
                    "owningRecordReference": record_reference,
                    "eventId": str(event_id),
                }
            )

        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status='BOOKING_IN_PROGRESS',
                    audit_state=CASE
                        WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS'
                        ELSE audit_state
                    END,
                    latest_activity_at_utc=now(),
                    updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id
                  AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "version": next_version,
            },
        )
        return {
            "journeyId": str(journey_id),
            "requirementRef": str(payload.requirementRef),
            "documentId": str(payload.documentId),
            "aggregateVersion": next_version,
            "decisions": results,
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.document-extraction-decisions:{journey_id}:{payload.documentId}",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
    )
    return BookingExtractionDecisionResponse.model_validate(body)
