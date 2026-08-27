from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, ConflictError, NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
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
    _parse_if_match,
    _stage_state,
)
from audit_core.uc03_booking_receipt_capture import (
    _RECEIPT_CAPTURE_MAP,
    _RECEIPT_DOCUMENT_TYPE,
    _write_receipt_capture,
)
from audit_core.uc03_pc_booking_documents import (
    BookingExtractionFieldDecision,
    _current_linked_evidence,
    _validate_unique_decisions,
)

router = APIRouter(tags=["uc03-pc-direct-review"])


class DirectDocumentReviewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirementRef: UUID
    documentId: UUID
    # A processed document may legitimately have no mapped Booking fields. The PC
    # still needs to be able to confirm that the source document itself was reviewed.
    fields: list[BookingExtractionFieldDecision] = Field(default_factory=list, max_length=100)


class DirectDocumentReviewDecision(BaseModel):
    fieldKey: str
    decision: Literal["APPROVED", "CORRECTED"]
    owningDomainKey: str
    owningRecordReference: str
    eventId: UUID


class DirectDocumentReviewResponse(BaseModel):
    journeyId: UUID
    requirementRef: UUID
    documentId: UUID
    aggregateVersion: int
    reviewEventId: UUID
    decisions: list[DirectDocumentReviewDecision]


class DirectReviewState(BaseModel):
    journeyId: UUID
    activeDocumentIds: list[UUID]
    reviewedDocumentIds: list[UUID]
    pendingDocumentIds: list[UUID]
    activeDocumentCount: int
    reviewedDocumentCount: int
    pendingDocumentCount: int
    reviewComplete: bool


class DirectPcVerificationResponse(BaseModel):
    journeyId: UUID
    pcVerificationStatus: Literal["PENDING", "VERIFIED"]
    aggregateVersion: int


def _active_document_ids(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[UUID]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT e.di_document_id
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_requirements jdr
              ON jdr.tenant_id=e.tenant_id
             AND jdr.journey_id=e.journey_id
             AND jdr.journey_document_requirement_id=e.journey_document_requirement_id
            WHERE e.tenant_id=:tenant_id
              AND e.journey_id=:journey_id
              AND e.association_status='ACTIVE'
              AND e.di_document_id IS NOT NULL
              AND upper(jdr.process_area)='BOOKING'
            ORDER BY e.di_document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalars().all()
    return list(rows)


def _reviewed_document_ids(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    active_document_ids: list[UUID],
) -> list[UUID]:
    if not active_document_ids:
        return []
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT (jwe.safe_payload->>'documentId')::uuid AS document_id
            FROM auditcore.journey_workflow_events jwe
            WHERE jwe.tenant_id=:tenant_id
              AND jwe.journey_id=:journey_id
              AND jwe.stage_code='BOOKING'
              AND jwe.event_type='BOOKING_DOCUMENT_REVIEWED'
              AND jwe.safe_payload ? 'documentId'
              AND (jwe.safe_payload->>'documentId')::uuid = ANY(:active_document_ids)
            ORDER BY document_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "active_document_ids": active_document_ids,
        },
    ).scalars().all()
    return list(rows)


def _direct_review_state(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> DirectReviewState:
    active = _active_document_ids(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    reviewed = _reviewed_document_ids(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        active_document_ids=active,
    )
    reviewed_set = set(reviewed)
    pending = [document_id for document_id in active if document_id not in reviewed_set]
    return DirectReviewState(
        journeyId=journey_id,
        activeDocumentIds=active,
        reviewedDocumentIds=reviewed,
        pendingDocumentIds=pending,
        activeDocumentCount=len(active),
        reviewedDocumentCount=len(reviewed),
        pendingDocumentCount=len(pending),
        reviewComplete=bool(active) and not pending,
    )


def _pc_state(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    for_update: bool = False,
):
    suffix = " FOR UPDATE" if for_update else ""
    row = connection.execute(
        text(
            """
            SELECT capture_completed_at_utc, pc_verification_status, version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """ + suffix
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Booking not found",
            detail="Booking stage not found for the requested Project.",
        )
    return row


def _existing_review_event(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
) -> UUID | None:
    return connection.execute(
        text(
            """
            SELECT event_id
            FROM auditcore.journey_workflow_events
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND stage_code='BOOKING'
              AND event_type='BOOKING_DOCUMENT_REVIEWED'
              AND safe_payload->>'documentId'=:document_id
            ORDER BY occurred_at_utc DESC, event_id DESC
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": str(document_id),
        },
    ).scalar_one_or_none()


@router.get(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/booking/direct-document-review",
    response_model=DirectReviewState,
)
def get_direct_document_review_state(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DirectReviewState:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _direct_review_state(connection, tenant_id=tenant_id, journey_id=journey_id)


@router.post(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/booking/direct-document-review",
    response_model=DirectDocumentReviewResponse,
)
def submit_direct_document_review(
    tenant_id: str,
    journey_id: UUID,
    payload: DirectDocumentReviewCommand,
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
) -> DirectDocumentReviewResponse:
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
                "decisions": [],
            }

        document_type_key = str(linked["document_type_key"] or "").strip().lower()
        allowed_source_fields = (
            set(_RECEIPT_CAPTURE_MAP)
            if document_type_key == _RECEIPT_DOCUMENT_TYPE
            else _SUPPORTED_PROPOSAL_FIELDS.get(document_type_key, set())
        )
        evidence_id: UUID = linked["evidence_id"]
        next_version = int(state["version_no"]) + 1
        results: list[dict[str, Any]] = []
        approved_count = 0
        corrected_count = 0

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

            event_type = (
                "BOOKING_EXTRACTION_APPROVED"
                if field.decision == "APPROVED"
                else "BOOKING_EXTRACTION_CORRECTED"
            )
            if field.decision == "APPROVED":
                approved_count += 1
            else:
                corrected_count += 1
            event_id = _append_workflow_event(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                event_type=event_type,
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
                "approvedFieldCount": approved_count,
                "correctedFieldCount": corrected_count,
            },
            aggregate_version=next_version,
        )

        # PC review changes only confirmed/corrected business values plus the PC
        # verification lifecycle. It must not manufacture a Booking business-state
        # transition or start the TL/PM audit lifecycle.
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
            "decisions": results,
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.direct-document-review:{journey_id}:{payload.documentId}",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
    )
    return DirectDocumentReviewResponse.model_validate(body)


@router.post(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/pc-verification/verify-direct",
    response_model=DirectPcVerificationResponse,
)
def verify_pc_booking_direct(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DirectPcVerificationResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _pc_state(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            for_update=True,
        )
        if int(state["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Booking version conflict",
                detail="Booking changed since it was loaded. Refresh the Booking and retry.",
            )
        if state["capture_completed_at_utc"] is None or state["pc_verification_status"] != "PENDING":
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="PC verification is not pending",
                detail="Submit Booking capture before completing PC verification.",
            )

        review_state = _direct_review_state(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        if not review_state.reviewComplete:
            raise ConflictError(
                error_code="VAC-CONFLICT-012",
                title="PC document review is incomplete",
                detail="Review every current Booking document before marking the Booking verified.",
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
                WHERE tenant_id=:tenant_id
                  AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_BOOKING_VERIFIED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "pcVerificationStatus": "VERIFIED",
                "reviewedDocumentCount": review_state.reviewedDocumentCount,
                "bookingBusinessStatusChanged": False,
                "tlReviewRequired": False,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "pcVerificationStatus": "VERIFIED",
            "aggregateVersion": next_version,
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.pc-verification.verify-direct:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version},
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return DirectPcVerificationResponse.model_validate(body)
