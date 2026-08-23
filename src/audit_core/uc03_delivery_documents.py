from __future__ import annotations

import json
from typing import Annotated, Any, Literal
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
    _authorize_security,
    _journey_context,
    _parse_if_match,
    _require_expected_version,
    _set_etag,
)
from audit_core.uc03_delivery_commands import (
    _append_delivery_event,
    _delivery_state,
    _machine_flag,
    _upsert_delivery_business_record,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/stages/DELIVERY/documents",
    tags=["uc03-delivery-documents"],
)

_DOCUMENT_NO_RULE = "DOC_REQUIRED_ANSWER_NO"


class DeliveryDocumentAssessmentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal["YES", "NO", "NA", "UNANSWERED"]
    evidenceId: UUID | None = None
    remarks: str | None = Field(default=None, max_length=4000)


class DeliveryDocumentAssessmentResponse(BaseModel):
    journeyId: UUID
    stage: Literal["DELIVERY"] = "DELIVERY"
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
    flagId: UUID | None = None


def _requirement(
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
                   jdr.requirement_level,
                   jdr.requirement_status,
                   jdr.condition_snapshot,
                   j.document_requirement_profile_version_id
            FROM auditcore.journey_document_requirements jdr
            JOIN auditcore.journeys j
              ON j.tenant_id=jdr.tenant_id AND j.journey_id=jdr.journey_id
            WHERE jdr.tenant_id=:tenant_id AND jdr.journey_id=:journey_id
              AND jdr.requirement_key=:requirement_key
              AND upper(jdr.process_area)='DELIVERY'
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
            title="Delivery document requirement not found",
            detail="The requested document requirement is not configured for this Delivery.",
        )
    return row


def _assessment(
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
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='DELIVERY' AND requirement_key=:requirement_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "requirement_key": requirement_key,
        },
    ).mappings().one_or_none()


def _applicability(requirement) -> tuple[str, str | None]:
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
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND evidence_id=:evidence_id
              AND journey_document_requirement_id=:requirement_id
              AND association_status='ACTIVE'
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
            detail="The selected evidence is not linked to this Delivery document requirement.",
        )


def _public(
    *,
    journey_id: UUID,
    requirement,
    assessment,
    aggregate_version: int,
    event_id: UUID | None = None,
    flag_id: UUID | None = None,
) -> dict[str, Any]:
    applicability_state, applicability_reason = _applicability(requirement)
    if assessment is None:
        answer = "UNANSWERED"
        evidence_id = None
        remarks = None
        assessment_version = None
    else:
        applicability_state = assessment["applicability_state"]
        applicability_reason = assessment["applicability_reason"]
        answer = assessment["answer"]
        evidence_id = assessment["evidence_id"]
        remarks = assessment["remarks"]
        assessment_version = assessment["version_no"]
    return DeliveryDocumentAssessmentResponse(
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
        flagId=flag_id,
    ).model_dump(mode="json")


def _resolve_known_applicability(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, str]]:
    """Resolve only frozen C2 conditions whose authoritative fact already exists.

    Exchange is frozen and typed in the Trade-In domain. Other conditional
    families remain profile-driven unless their condition snapshot already has a
    resolved applicability state; C2 must not infer unresolved business logic.
    """
    details = connection.execute(
        text(
            """
            SELECT details FROM auditcore.trade_in_cases
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    exchange_taken = details.get("exchangeTaken") if isinstance(details, dict) else None

    rows = connection.execute(
        text(
            """
            SELECT journey_document_requirement_id, requirement_key,
                   requirement_status, condition_snapshot
            FROM auditcore.journey_document_requirements
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND upper(process_area)='DELIVERY'
              AND requirement_level='CONDITIONAL'
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    changes: list[dict[str, str]] = []
    for row in rows:
        snapshot = dict(row["condition_snapshot"] or {})
        condition_key = str(snapshot.get("conditionKey") or "").strip().lower()
        if condition_key not in {"exchangetaken", "exchange_taken"}:
            continue
        if not isinstance(exchange_taken, bool):
            continue
        state = "APPLICABLE" if exchange_taken else "NOT_APPLICABLE"
        previous = str(snapshot.get("applicabilityState") or "UNRESOLVED")
        reason = f"exchangeTaken={'Yes' if exchange_taken else 'No'}"
        if previous == state and snapshot.get("applicabilityReason") == reason:
            continue
        snapshot["applicabilityState"] = state
        snapshot["applicabilityReason"] = reason
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_document_requirements
                SET requirement_status=:requirement_status,
                    condition_snapshot=CAST(:snapshot AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id
                  AND journey_document_requirement_id=:requirement_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "requirement_id": row["journey_document_requirement_id"],
                "requirement_status": "PENDING" if exchange_taken else "NOT_APPLICABLE",
                "snapshot": json.dumps(snapshot),
            },
        )
        changes.append(
            {
                "requirementKey": row["requirement_key"],
                "previousState": previous,
                "applicabilityState": state,
                "reason": reason,
            }
        )
    return changes


@router.get("", response_model=list[DeliveryDocumentAssessmentResponse])
def list_delivery_documents(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[DeliveryDocumentAssessmentResponse]:
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
    state = _delivery_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    if state is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Delivery has not started",
            detail="Start Delivery before viewing the Delivery checklist.",
        )
    _resolve_known_applicability(
        connection, tenant_id=tenant_id, journey_id=journey_id
    )
    requirements = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id,
                   jdr.requirement_key, jdr.document_type_key,
                   jdr.requirement_level, jdr.requirement_status,
                   jdr.condition_snapshot,
                   j.document_requirement_profile_version_id
            FROM auditcore.journey_document_requirements jdr
            JOIN auditcore.journeys j
              ON j.tenant_id=jdr.tenant_id AND j.journey_id=jdr.journey_id
            WHERE jdr.tenant_id=:tenant_id AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='DELIVERY'
            ORDER BY jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        DeliveryDocumentAssessmentResponse.model_validate(
            _public(
                journey_id=journey_id,
                requirement=requirement,
                assessment=_assessment(
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


@router.put("/{requirement_key}", response_model=DeliveryDocumentAssessmentResponse)
def record_delivery_document(
    tenant_id: str,
    journey_id: UUID,
    requirement_key: str,
    payload: DeliveryDocumentAssessmentCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeliveryDocumentAssessmentResponse:
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

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _delivery_state(
            connection, tenant_id=tenant_id, journey_id=journey_id, for_update=True
        )
        _require_expected_version(state, expected_version)
        if state is None:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Delivery has not started",
                detail="Start Delivery before recording Delivery document answers.",
            )
        _resolve_known_applicability(
            connection, tenant_id=tenant_id, journey_id=journey_id
        )
        requirement = _requirement(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirement_key=requirement_key,
            for_update=True,
        )
        applicability_state, applicability_reason = _applicability(requirement)
        if applicability_state == "UNRESOLVED":
            raise ConflictError(
                error_code="VAC-CONFLICT-007",
                title="Document applicability is pending",
                detail="This conditional Delivery document cannot be assessed until applicability is resolved.",
            )
        if payload.answer == "NA" and applicability_state != "NOT_APPLICABLE":
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Business validation failed",
                detail="NA is allowed only when the Delivery document is not applicable.",
            )
        if payload.answer != "NA" and applicability_state == "NOT_APPLICABLE":
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Business validation failed",
                detail="A not-applicable Delivery document can only be recorded as NA.",
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
                SET requirement_status=:status, updated_at_utc=now()
                WHERE tenant_id=:tenant_id
                  AND journey_document_requirement_id=:requirement_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "requirement_id": requirement["journey_document_requirement_id"],
                "status": requirement_status,
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
                    :tenant_id, :journey_id, 'DELIVERY', :requirement_id,
                    :requirement_key, :profile_version_id,
                    :applicability_state, :applicability_reason, :answer,
                    :evidence_id, :remarks, :actor_id, :actor_role, now()
                )
                ON CONFLICT (tenant_id, journey_id, stage_code, requirement_key)
                DO UPDATE SET
                    journey_document_requirement_id=EXCLUDED.journey_document_requirement_id,
                    document_requirement_profile_version_id=EXCLUDED.document_requirement_profile_version_id,
                    applicability_state=EXCLUDED.applicability_state,
                    applicability_reason=EXCLUDED.applicability_reason,
                    answer=EXCLUDED.answer,
                    evidence_id=EXCLUDED.evidence_id,
                    remarks=EXCLUDED.remarks,
                    answered_by_actor_id=EXCLUDED.answered_by_actor_id,
                    answered_by_role=EXCLUDED.answered_by_role,
                    answered_at_utc=EXCLUDED.answered_at_utc,
                    version_no=auditcore.journey_document_assessments.version_no+1
                RETURNING applicability_state, applicability_reason, answer,
                          evidence_id, remarks, version_no
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "requirement_id": requirement["journey_document_requirement_id"],
                "requirement_key": requirement_key,
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
        next_business_status = (
            "DELIVERY_IN_PROGRESS"
            if state["business_status"] == "DELIVERY_STARTED"
            else state["business_status"]
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status=:business_status,
                    audit_state=CASE
                        WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS'
                        ELSE audit_state
                    END,
                    latest_activity_at_utc=now(), updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='DELIVERY'
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "business_status": next_business_status,
                "version": next_version,
            },
        )
        if next_business_status == "DELIVERY_IN_PROGRESS":
            _upsert_delivery_business_record(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                business_status="DELIVERY_IN_PROGRESS",
                actor_id=human_principal.subject,
                completed=False,
            )

        flag_id: UUID | None = None
        if payload.answer == "NO":
            flag_id = _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code="DELIVERY",
                rule_key=f"{_DOCUMENT_NO_RULE}:{requirement_key}",
                finding_type="REQUIRED_DOCUMENT_ANSWER_NO",
                severity=(
                    "HIGH" if requirement["requirement_level"] == "REQUIRED" else "MEDIUM"
                ),
                title="Delivery document answered No",
                description=f"Requirement {requirement_key} was explicitly answered No.",
                correlation_id=correlation_id,
                safe_payload={"requirementKey": requirement_key},
            )
        event_id = _append_delivery_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="DELIVERY_DOCUMENT_ASSESSED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "requirementKey": requirement_key,
                "answer": payload.answer,
                "evidenceId": str(payload.evidenceId) if payload.evidenceId else None,
            },
            aggregate_version=next_version,
        )
        refreshed_requirement = _requirement(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirement_key=requirement_key,
        )
        return _public(
            journey_id=journey_id,
            requirement=refreshed_requirement,
            assessment=assessment,
            aggregate_version=next_version,
            event_id=event_id,
            flag_id=flag_id,
        )

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.delivery.document:{journey_id}:{requirement_key}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version, **payload.model_dump(mode="json")},
        execute=execute,
    )
    _set_etag(response, body)
    return DeliveryDocumentAssessmentResponse.model_validate(body)
