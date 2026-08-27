from __future__ import annotations

import json
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
from audit_core.uc03_booking_capture import (
    _PROPOSAL_CAPTURE_MAP,
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
    actor_id: str,
    fields: list[DirectExtractedField],
) -> None:
    if not fields:
        return
    rows = []
    for field in fields:
        modified = field.modifiedValue is not None
        rows.append(
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "evidence_id": evidence_id,
                "document_id": document_id,
                "source_fact_ref": field.sourceFactRef,
                "source_fact_version": field.sourceFactVersion,
                "field_key": field.fieldKey.strip().lower(),
                "extracted_value": json.dumps(field.extractedValue, default=str),
                "modified_value": (
                    json.dumps(field.modifiedValue, default=str) if modified else None
                ),
                "confidence_score": field.confidenceScore,
                "modified_by_actor_id": actor_id if modified else None,
            }
        )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, evidence_id, di_document_id,
                source_fact_ref, source_fact_version, field_key,
                extracted_value, modified_value, confidence_score,
                modified_by_actor_id, modified_at_utc
            ) VALUES (
                :tenant_id, :journey_id, :evidence_id, :document_id,
                :source_fact_ref, :source_fact_version, :field_key,
                CAST(:extracted_value AS jsonb), CAST(:modified_value AS jsonb),
                :confidence_score, :modified_by_actor_id,
                CASE WHEN :modified_by_actor_id IS NULL THEN NULL ELSE now() END
            )
            ON CONFLICT (
                tenant_id, journey_id, di_document_id,
                source_fact_ref, source_fact_version
            ) DO UPDATE SET
                evidence_id=EXCLUDED.evidence_id,
                field_key=EXCLUDED.field_key,
                extracted_value=EXCLUDED.extracted_value,
                modified_value=EXCLUDED.modified_value,
                confidence_score=EXCLUDED.confidence_score,
                modified_by_actor_id=EXCLUDED.modified_by_actor_id,
                modified_at_utc=EXCLUDED.modified_at_utc,
                updated_at_utc=now()
            """
        ),
        rows,
    )


def _project_known_field(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    document_type_key: str,
    field: DirectExtractedField,
) -> tuple[str, str] | None:
    source_field_key = field.fieldKey.strip().lower()
    receipt_capture_key = (
        _RECEIPT_CAPTURE_MAP.get(source_field_key)
        if document_type_key == _RECEIPT_DOCUMENT_TYPE
        else None
    )
    capture_key = receipt_capture_key or _PROPOSAL_CAPTURE_MAP.get(source_field_key)
    if capture_key is None:
        return None

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
    return _write_typed_capture(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        field_key=capture_key,
        value=value,
        source_evidence_id=evidence_id,
    )


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
        _store_fields(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_id=evidence_id,
            document_id=payload.documentId,
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
                        document_type_key=document_type_key,
                        field=field,
                    )
                if projected is not None:
                    projected_count += 1
            except Exception:
                projection_failure_count += 1
                logger.warning(
                    "UC03 typed projection failed after generic DI field persistence",
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
                "storedFieldCount": len(payload.fields),
                "modifiedFieldCount": modified_count,
                "projectedFieldCount": projected_count,
                "projectionFailureCount": projection_failure_count,
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
            "storedFieldCount": len(payload.fields),
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
