from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, ConflictError, NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _authorize_security,
    _journey_context,
    _parse_if_match,
    _require_expected_version,
    _set_etag,
    _stage_state,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/stages/{stage_code}/documents",
    tags=["uc03-documents"],
)


class DocumentAssessmentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal["YES", "NO", "NA", "UNANSWERED"]
    evidenceId: UUID | None = None
    remarks: str | None = Field(default=None, max_length=4000)


class DocumentAssessmentResponse(BaseModel):
    journeyId: UUID
    stage: Literal["BOOKING"] = "BOOKING"
    requirementKey: str
    documentTypeKey: str
    requirementLevel: str
    requirementStatus: str
    applicabilityState: Literal["APPLICABLE", "NOT_APPLICABLE", "UNRESOLVED"]
    applicabilityReason: str | None
    answer: Literal["YES", "NO", "NA", "UNANSWERED"]
    evidenceId: UUID | None
    remarks: str | None
    assessmentVersion: int | None
    aggregateVersion: int
    eventId: UUID | None = None


def _require_booking_stage(stage_code: str) -> None:
    if stage_code.upper() != "BOOKING":
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail="Only Booking document assessment is available in the current checkpoint.",
        )


def _requirement_row(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirement_key: str,
    for_update: bool = False,
):
    lock_clause = " FOR UPDATE" if for_update else ""
    row = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id,
                   jdr.requirement_key,
                   jdr.document_type_key,
                   jdr.process_area,
                   jdr.requirement_level,
                   jdr.requirement_status,
                   jdr.condition_snapshot,
                   j.document_requirement_profile_version_id
            FROM auditcore.journey_document_requirements jdr
            JOIN auditcore.journeys j
              ON j.tenant_id = jdr.tenant_id
             AND j.journey_id = jdr.journey_id
            WHERE jdr.tenant_id = :tenant_id
              AND jdr.journey_id = :journey_id
              AND jdr.requirement_key = :requirement_key
              AND upper(jdr.process_area) = 'BOOKING'
            """
            + lock_clause
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "requirement_key": requirement_key,
        },
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Booking document requirement not found",
            detail="The requested document requirement is not configured for this Booking.",
        )
    return row


def _effective_applicability(requirement) -> tuple[str, str | None]:
    if requirement["requirement_status"] == "NOT_APPLICABLE":
        snapshot = requirement["condition_snapshot"] or {}
        reason = snapshot.get("applicabilityReason") if isinstance(snapshot, dict) else None
        return "NOT_APPLICABLE", reason if isinstance(reason, str) else None

    if requirement["requirement_level"] != "CONDITIONAL":
        return "APPLICABLE", None

    snapshot = requirement["condition_snapshot"] or {}
    if isinstance(snapshot, dict):
        state = snapshot.get("applicabilityState")
        reason = snapshot.get("applicabilityReason")
        if state in {"APPLICABLE", "NOT_APPLICABLE"}:
            return state, reason if isinstance(reason, str) else None
    return "UNRESOLVED", None


def _assessment_row(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirement_key: str,
):
    return connection.execute(
        text(
            """
            SELECT applicability_state, applicability_reason, answer,
                   evidence_id, remarks, version_no
            FROM auditcore.journey_document_assessments
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND stage_code = 'BOOKING'
              AND requirement_key = :requirement_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "requirement_key": requirement_key,
        },
    ).mappings().one_or_none()


def _validate_evidence(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirement_id: UUID,
    evidence_id: UUID | None,
) -> None:
    if evidence_id is None:
        return
    exists = connection.execute(
        text(
            """
            SELECT 1
            FROM auditcore.evidence
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND evidence_id = :evidence_id
              AND journey_document_requirement_id = :requirement_id
              AND association_status = 'ACTIVE'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": evidence_id,
            "requirement_id": requirement_id,
        },
    ).scalar_one_or_none()
    if exists is None:
        raise AuditCoreError(
            error_code="VAC-VAL-003",
            status_code=400,
            title="Unsupported evidence",
            detail="The selected evidence is not linked to this Booking document requirement.",
        )


def _require_active_booking(state) -> None:
    if state is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking has not started",
            detail="Start the Booking before recording document assessment.",
        )
    if state["business_status"] not in {"BOOKING_STARTED", "BOOKING_IN_PROGRESS"}:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking state conflict",
            detail="The current Booking state does not allow document assessment changes.",
        )


def _public_response(
    *,
    journey_id: UUID,
    requirement,
    assessment,
    aggregate_version: int,
    event_id: UUID | None = None,
) -> dict:
    applicability_state, applicability_reason = _effective_applicability(requirement)
    if assessment is not None:
        applicability_state = assessment["applicability_state"]
        applicability_reason = assessment["applicability_reason"]
        answer = assessment["answer"]
        evidence_id = assessment["evidence_id"]
        remarks = assessment["remarks"]
        assessment_version = assessment["version_no"]
    else:
        answer = "UNANSWERED"
        evidence_id = None
        remarks = None
        assessment_version = None
    return DocumentAssessmentResponse(
        journeyId=journey_id,
        requirementKey=requirement["requirement_key"],
        documentTypeKey=requirement["document_type_key"],
        requirementLevel=requirement["requirement_level"],
        requirementStatus=requirement["requirement_status"],
        applicabilityState=applicability_state,
        applicabilityReason=applicability_reason,
        answer=answer,
        evidenceId=evidence_id,
        remarks=remarks,
        assessmentVersion=assessment_version,
        aggregateVersion=aggregate_version,
        eventId=event_id,
    ).model_dump(mode="json")


@router.get("", response_model=list[DocumentAssessmentResponse])
def list_document_assessments(
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[DocumentAssessmentResponse]:
    _require_booking_stage(stage_code)
    _authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    _journey_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )
    state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    _require_active_booking(state)

    requirements = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id,
                   jdr.requirement_key,
                   jdr.document_type_key,
                   jdr.process_area,
                   jdr.requirement_level,
                   jdr.requirement_status,
                   jdr.condition_snapshot,
                   j.document_requirement_profile_version_id
            FROM auditcore.journey_document_requirements jdr
            JOIN auditcore.journeys j
              ON j.tenant_id = jdr.tenant_id
             AND j.journey_id = jdr.journey_id
            WHERE jdr.tenant_id = :tenant_id
              AND jdr.journey_id = :journey_id
              AND upper(jdr.process_area) = 'BOOKING'
            ORDER BY jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    return [
        DocumentAssessmentResponse.model_validate(
            _public_response(
                journey_id=journey_id,
                requirement=requirement,
                assessment=_assessment_row(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    requirement_key=requirement["requirement_key"],
                ),
                aggregate_version=int(state["version_no"]),
            )
        )
        for requirement in requirements
    ]


@router.put("/{requirement_key}", response_model=DocumentAssessmentResponse)
def record_document_assessment(
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    requirement_key: str,
    payload: DocumentAssessmentCommand,
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
) -> DocumentAssessmentResponse:
    _require_booking_stage(stage_code)
    _authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    context = _journey_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_expected_version(state, expected_version)
        _require_active_booking(state)
        requirement = _requirement_row(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirement_key=requirement_key,
            for_update=True,
        )
        applicability_state, applicability_reason = _effective_applicability(requirement)
        if applicability_state == "UNRESOLVED":
            raise ConflictError(
                error_code="VAC-CONFLICT-007",
                title="Document applicability is pending",
                detail="This conditional Booking document cannot be assessed until applicability is resolved.",
            )
        if payload.answer == "NA" and applicability_state != "NOT_APPLICABLE":
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Business validation failed",
                detail="NA is allowed only after the document requirement is resolved as not applicable.",
            )
        if payload.answer != "NA" and applicability_state == "NOT_APPLICABLE":
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Business validation failed",
                detail="A not-applicable document requirement can only be recorded as NA.",
            )
        _validate_evidence(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirement_id=requirement["journey_document_requirement_id"],
            evidence_id=payload.evidenceId,
        )

        requirement_status = "PENDING"
        if payload.answer == "NA":
            requirement_status = "NOT_APPLICABLE"
        elif payload.answer == "YES" and payload.evidenceId is not None:
            requirement_status = "SATISFIED"

        connection.execute(
            text(
                """
                UPDATE auditcore.journey_document_requirements
                SET requirement_status = :requirement_status,
                    updated_at_utc = now()
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND journey_document_requirement_id = :requirement_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "requirement_id": requirement["journey_document_requirement_id"],
                "requirement_status": requirement_status,
            },
        )

        assessment = connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_assessments (
                    tenant_id, journey_id, stage_code,
                    journey_document_requirement_id, requirement_key,
                    document_requirement_profile_version_id,
                    applicability_state, applicability_reason, answer,
                    evidence_id, remarks, answered_by_actor_id,
                    answered_by_role, answered_at_utc
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING',
                    :requirement_id, :requirement_key,
                    :profile_version_id,
                    :applicability_state, :applicability_reason, :answer,
                    :evidence_id, :remarks, :actor_id,
                    :actor_role, now()
                )
                ON CONFLICT (tenant_id, journey_id, stage_code, requirement_key)
                DO UPDATE SET
                    journey_document_requirement_id = EXCLUDED.journey_document_requirement_id,
                    document_requirement_profile_version_id = EXCLUDED.document_requirement_profile_version_id,
                    applicability_state = EXCLUDED.applicability_state,
                    applicability_reason = EXCLUDED.applicability_reason,
                    answer = EXCLUDED.answer,
                    evidence_id = EXCLUDED.evidence_id,
                    remarks = EXCLUDED.remarks,
                    answered_by_actor_id = EXCLUDED.answered_by_actor_id,
                    answered_by_role = EXCLUDED.answered_by_role,
                    answered_at_utc = EXCLUDED.answered_at_utc,
                    version_no = auditcore.journey_document_assessments.version_no + 1
                RETURNING applicability_state, applicability_reason, answer,
                          evidence_id, remarks, version_no
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "requirement_id": requirement["journey_document_requirement_id"],
                "requirement_key": requirement["requirement_key"],
                "profile_version_id": requirement[
                    "document_requirement_profile_version_id"
                ],
                "applicability_state": applicability_state,
                "applicability_reason": applicability_reason,
                "answer": payload.answer,
                "evidence_id": payload.evidenceId,
                "remarks": (payload.remarks or "").strip() or None,
                "actor_id": human_principal.subject,
                "actor_role": context["operating_role"],
            },
        ).mappings().one()

        next_version = int(state["version_no"]) + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status = CASE
                        WHEN business_status = 'BOOKING_STARTED'
                            THEN 'BOOKING_IN_PROGRESS'
                        ELSE business_status
                    END,
                    audit_state = CASE
                        WHEN audit_state = 'NOT_STARTED' THEN 'IN_PROGRESS'
                        ELSE audit_state
                    END,
                    latest_activity_at_utc = now(),
                    updated_at_utc = now(),
                    version_no = :next_version
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND stage_code = 'BOOKING'
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "next_version": next_version,
            },
        )
        event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="DOCUMENT_ASSESSMENT_RECORDED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "requirementKey": requirement["requirement_key"],
                "answer": payload.answer,
                "evidenceLinked": payload.evidenceId is not None,
            },
            aggregate_version=next_version,
        )
        refreshed_requirement = dict(requirement)
        refreshed_requirement["requirement_status"] = requirement_status
        return _public_response(
            journey_id=journey_id,
            requirement=refreshed_requirement,
            assessment=assessment,
            aggregate_version=next_version,
            event_id=event_id,
        )

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.document-assessment:{journey_id}:{requirement_key}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            **payload.model_dump(mode="json"),
        },
        execute=execute,
    )
    _set_etag(response, body)
    return DocumentAssessmentResponse.model_validate(body)
