from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_attribute_mapping import spec_for_field
from audit_core.uc03_attribute_resolution import apply_supported_operational_attribute
from audit_core.uc03_booking_capture import (
    _require_active_booking,
    _scope,
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
from audit_core.uc03_di_core_persistence import (
    ReviewedDiField,
    persist_reviewed_di_fields,
)
from audit_core.uc03_pc_booking_documents import _current_linked_evidence
from audit_core.uc03_pc_direct_review import _existing_review_event

logger = logging.getLogger(__name__)
router = APIRouter(tags=["uc03-pc-generic-review"])


class DirectExtractedField(BaseModel):
    """One DI field as shown to the PC, with an optional PC modification."""

    model_config = ConfigDict(extra="forbid")

    fieldKey: str = Field(min_length=1, max_length=160)
    sourceFactRef: UUID
    sourceFactVersion: int = Field(gt=0)
    extractedValue: Any | None = None
    modifiedValue: Any | None = None
    confidenceScore: float | None = Field(default=None, ge=0, le=1)


class DirectDocumentFieldReviewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirementRef: UUID
    documentId: UUID
    fields: list[DirectExtractedField] = Field(default_factory=list, max_length=500)


class DirectDocumentFieldReviewResponse(BaseModel):
    journeyId: UUID
    requirementRef: UUID
    documentId: UUID
    aggregateVersion: int
    reviewEventId: UUID
    storedFieldCount: int
    modifiedFieldCount: int
    projectedFieldCount: int
    projectionFailureCount: int


def _validate_unique_fields(fields: list[DirectExtractedField]) -> None:
    seen: set[tuple[UUID, int]] = set()
    for field in fields:
        key = (field.sourceFactRef, field.sourceFactVersion)
        if key in seen:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Duplicate extraction field",
                detail="A DI source fact version may appear only once in a document review.",
            )
        seen.add(key)


def _stored_field_count(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
) -> int:
    return int(
        connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:tenant_id
                  AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                  AND di_document_id=:document_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "document_id": document_id,
            },
        ).scalar_one()
    )


def _store_fields(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    document_id: UUID,
    document_type_key: str,
    actor_id: str,
    fields: list[DirectExtractedField],
) -> int:
    """Persist every populated direct-review DI field before typed projection."""

    reviewed_fields = [
        ReviewedDiField(
            document_id=document_id,
            field_key=field.fieldKey.strip().lower(),
            source_fact_version=field.sourceFactVersion,
            evidence_id=evidence_id,
            source_fact_ref=field.sourceFactRef,
            source_document_type_key=document_type_key,
            extracted_value=field.extractedValue,
            modified_value=field.modifiedValue,
            effective_value=(
                field.modifiedValue
                if field.modifiedValue is not None
                else field.extractedValue
            ),
            confidence_score=field.confidenceScore,
            confidence_scale=(
                "UNIT_INTERVAL" if field.confidenceScore is not None else None
            ),
            is_modified=field.modifiedValue is not None,
        )
        for field in fields
    ]
    return persist_reviewed_di_fields(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="BOOKING",
        actor_id=actor_id,
        fields=reviewed_fields,
    )


def _project_known_field(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    document_id: UUID,
    document_type_key: str,
    actor_id: str,
    field: DirectExtractedField,
) -> tuple[str, str] | None:
    source_field_key = field.fieldKey.strip().lower()
    receipt_capture_key = (
        _RECEIPT_CAPTURE_MAP.get(source_field_key)
        if document_type_key == _RECEIPT_DOCUMENT_TYPE
        else None
    )
    value = field.modifiedValue if field.modifiedValue is not None else field.extractedValue
    if receipt_capture_key is not None:
        return _write_receipt_capture(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            capture_key=receipt_capture_key,
            value=value,
            source_evidence_id=evidence_id,
        )

    spec = spec_for_field(source_field_key)
    if spec is None:
        return None
    application = apply_supported_operational_attribute(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        spec=spec,
        value=value,
        actor_id=actor_id,
        source_document_type_key=document_type_key,
        source_field_key=source_field_key,
        source_evidence_id=evidence_id,
    )
    if application is None:
        return None
    return application[0], application[1]


@router.post(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/booking/direct-document-review-fields",
    response_model=DirectDocumentFieldReviewResponse,
)
def submit_direct_document_field_review(
    tenant_id: str,
    journey_id: UUID,
    payload: DirectDocumentFieldReviewCommand,
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
) -> DirectDocumentFieldReviewResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    correlation_id = get_correlation_id(request)
    _validate_unique_fields(payload.fields)

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
        existing_event = _existing_review_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=payload.documentId,
        )
        if existing_event is not None:
            return {
                "journeyId": str(journey_id),
                "requirementRef": str(payload.requirementRef),
                "documentId": str(payload.documentId),
                "aggregateVersion": int(state["version_no"]),
                "reviewEventId": str(existing_event),
                "storedFieldCount": _stored_field_count(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    document_id=payload.documentId,
                ),
                "modifiedFieldCount": 0,
                "projectedFieldCount": 0,
                "projectionFailureCount": 0,
            }

        evidence_id: UUID = linked["evidence_id"]
        document_type_key = str(linked["document_type_key"] or "").strip().lower()
        stored_count = _store_fields(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_id=evidence_id,
            document_id=payload.documentId,
            document_type_key=document_type_key,
            actor_id=human_principal.subject,
            fields=payload.fields,
        )

        next_version = int(state["version_no"]) + 1
        modified_count = sum(field.modifiedValue is not None for field in payload.fields)
        projected_count = 0
        projection_failure_count = 0

        for index, field in enumerate(payload.fields):
            source_field_key = field.fieldKey.strip().lower()
            try:
                with connection.begin_nested():
                    projected = _project_known_field(
                        connection,
                        tenant_id=tenant_id,
                        journey_id=journey_id,
                        evidence_id=evidence_id,
                        document_id=payload.documentId,
                        document_type_key=document_type_key,
                        actor_id=human_principal.subject,
                        field=field,
                    )
                if projected is not None:
                    projected_count += 1
            except Exception:
                projection_failure_count += 1
                logger.warning(
                    "UC03 typed projection failed after document review",
                    exc_info=True,
                    extra={
                        "tenant_id": tenant_id,
                        "journey_id": str(journey_id),
                        "document_id": str(payload.documentId),
                        "field_key": source_field_key,
                    },
                )

            if field.modifiedValue is not None:
                _append_workflow_event(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    event_type="BOOKING_EXTRACTION_CORRECTED",
                    source_kind="HUMAN",
                    actor_id=human_principal.subject,
                    actor_role_snapshot=context["operating_role"],
                    idempotency_key=f"{idempotency_key}:modified:{index}",
                    correlation_id=correlation_id,
                    safe_payload={
                        "requirementRef": str(payload.requirementRef),
                        "documentId": str(payload.documentId),
                        "fieldKey": source_field_key,
                        "sourceFactRef": str(field.sourceFactRef),
                        "sourceFactVersion": field.sourceFactVersion,
                    },
                    aggregate_version=next_version,
                )

        review_event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="BOOKING_DOCUMENT_REVIEWED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=f"{idempotency_key}:document",
            correlation_id=correlation_id,
            safe_payload={
                "requirementRef": str(payload.requirementRef),
                "documentId": str(payload.documentId),
                "reviewedFieldCount": len(payload.fields),
                "storedFieldCount": stored_count,
                "storedCorrectionCount": modified_count,
                "modifiedFieldCount": modified_count,
                "projectedFieldCount": projected_count,
                "projectionFailureCount": projection_failure_count,
                "rawDiValuesCopied": True,
            },
            aggregate_version=next_version,
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET latest_activity_at_utc=now(),
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
            "reviewEventId": str(review_event_id),
            "storedFieldCount": stored_count,
            "modifiedFieldCount": modified_count,
            "projectedFieldCount": projected_count,
            "projectionFailureCount": projection_failure_count,
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=(
            f"uc03.booking.direct-document-review-fields:{journey_id}:{payload.documentId}"
        ),
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
    )
    return DirectDocumentFieldReviewResponse.model_validate(body)
