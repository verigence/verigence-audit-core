from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError, authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}", tags=["audit-review"])


class AuditStateResponse(BaseModel):
    journeyId: UUID
    auditState: str
    auditOutcome: str
    auditStartedAtUtc: datetime | None
    pcSubmittedAtUtc: datetime | None
    reviewCompletedAtUtc: datetime | None
    versionNo: int


class ReviewDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["BREACH", "NO_BREACH", "SEND_BACK"]
    reviewerRoleCode: Literal["TL", "PM"]
    remarks: str | None = None


class ReviewDecisionResponse(BaseModel):
    reviewDecisionId: UUID
    decision: str
    reviewerActorId: str
    reviewerRoleCode: str | None
    remarks: str | None
    decidedAtUtc: datetime


def _journey(connection: Connection, tenant_id: str, journey_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id, audit_state, audit_outcome,
                   audit_started_at_utc, pc_submitted_at_utc,
                   review_completed_at_utc, version_no
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Journey not found",
            detail="Journey not found for the requested tenant.",
        )
    return row


def _scope(
    connection: Connection,
    principal: Principal,
    *,
    tenant_id: str,
    journey_id: UUID,
):
    journey = _journey(connection, tenant_id, journey_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )
    return journey


def _state_response(journey_id: UUID, row) -> AuditStateResponse:
    return AuditStateResponse(
        journeyId=journey_id,
        auditState=row["audit_state"],
        auditOutcome=row["audit_outcome"],
        auditStartedAtUtc=row["audit_started_at_utc"],
        pcSubmittedAtUtc=row["pc_submitted_at_utc"],
        reviewCompletedAtUtc=row["review_completed_at_utc"],
        versionNo=row["version_no"],
    )


def _require_reviewer_role(
    connection: Connection,
    principal: Principal,
    *,
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
    role_code: str,
) -> None:
    assigned = connection.execute(
        text(
            """
            SELECT 1
            FROM auditcore.business_assignments
            WHERE tenant_id = :tenant_id
              AND security_actor_id = :actor_id
              AND business_role_code = :role_code
              AND assignment_status = 'ACTIVE'
              AND effective_from <= now()
              AND (effective_to IS NULL OR effective_to >= now())
              AND (
                    dealer_id IS NULL
                    OR (
                        dealer_id = :dealer_id
                        AND (outlet_id IS NULL OR outlet_id = :outlet_id)
                    )
              )
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": principal.subject,
            "role_code": role_code,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
        },
    ).scalar_one_or_none()
    if assigned is None:
        raise AuthorizationError(
            error_code="VAC-AUTH-004",
            status_code=403,
            title="Reviewer role scope denied",
        )


@router.get("/audit", response_model=AuditStateResponse)
def get_audit_state(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> AuditStateResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.read")
    set_tenant_context(connection, tenant_id)
    row = _scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    return _state_response(journey_id, row)


@router.post("/audit/start", response_model=AuditStateResponse)
def start_audit(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> AuditStateResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.update")
    set_tenant_context(connection, tenant_id)
    row = _scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    if row["audit_state"] not in {"NOT_STARTED", "IN_PROGRESS"}:
        raise AuditCoreError(
            error_code="VAC-CONFLICT-002",
            status_code=409,
            title="Invalid audit state transition",
            detail="Audit can be started only from NOT_STARTED or IN_PROGRESS.",
        )
    connection.execute(
        text(
            """
            UPDATE auditcore.journeys
            SET audit_state = 'IN_PROGRESS',
                audit_started_at_utc = COALESCE(audit_started_at_utc, now()),
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    )
    return _state_response(journey_id, _journey(connection, tenant_id, journey_id))


@router.post("/audit/submit", response_model=AuditStateResponse)
def submit_audit(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> AuditStateResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.submit")
    set_tenant_context(connection, tenant_id)
    row = _scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    if row["audit_state"] not in {"IN_PROGRESS", "SENT_BACK"}:
        raise AuditCoreError(
            error_code="VAC-CONFLICT-002",
            status_code=409,
            title="Invalid audit state transition",
            detail="Audit can be submitted only from IN_PROGRESS or SENT_BACK.",
        )
    connection.execute(
        text(
            """
            UPDATE auditcore.journeys
            SET audit_state = 'PC_SUBMITTED',
                pc_submitted_at_utc = now(),
                audit_outcome = 'PENDING',
                review_completed_at_utc = NULL,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    )
    return _state_response(journey_id, _journey(connection, tenant_id, journey_id))


def _decision_response(row) -> ReviewDecisionResponse:
    return ReviewDecisionResponse(
        reviewDecisionId=row["review_decision_id"],
        decision=row["decision"],
        reviewerActorId=row["reviewer_actor_id"],
        reviewerRoleCode=row["reviewer_role_code"],
        remarks=row["remarks"],
        decidedAtUtc=row["decided_at_utc"],
    )


@router.get("/review-decisions", response_model=list[ReviewDecisionResponse])
def list_review_decisions(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[ReviewDecisionResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.review.read")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    rows = connection.execute(
        text(
            """
            SELECT review_decision_id, decision, reviewer_actor_id,
                   reviewer_role_code, remarks, decided_at_utc
            FROM auditcore.review_decisions
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY decided_at_utc, review_decision_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [_decision_response(row) for row in rows]


@router.post("/review-decisions", response_model=ReviewDecisionResponse, status_code=201)
def create_review_decision(
    tenant_id: str,
    journey_id: UUID,
    payload: ReviewDecisionInput,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ReviewDecisionResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.review.decide")
    set_tenant_context(connection, tenant_id)
    journey = _scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    _require_reviewer_role(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
        role_code=payload.reviewerRoleCode,
    )
    if journey["audit_state"] not in {"PC_SUBMITTED", "TL_REVIEW", "PM_REVIEW"}:
        raise AuditCoreError(
            error_code="VAC-CONFLICT-002",
            status_code=409,
            title="Invalid audit review state",
            detail="Review decision requires submitted or active review work.",
        )

    decision_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.review_decisions (
                tenant_id, journey_id, decision, reviewer_actor_id,
                reviewer_role_code, remarks
            ) VALUES (
                :tenant_id, :journey_id, :decision, :actor_id,
                :role_code, :remarks
            ) RETURNING review_decision_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "decision": payload.decision,
            "actor_id": principal.subject,
            "role_code": payload.reviewerRoleCode,
            "remarks": payload.remarks,
        },
    ).scalar_one()

    if payload.decision == "SEND_BACK":
        next_state = "SENT_BACK"
        outcome = "PENDING"
        completed_at = None
    else:
        next_state = "REVIEW_COMPLETE"
        outcome = payload.decision
        completed_at = datetime.now().astimezone()

    connection.execute(
        text(
            """
            UPDATE auditcore.journeys
            SET audit_state = :audit_state,
                audit_outcome = :audit_outcome,
                review_completed_at_utc = :completed_at,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "audit_state": next_state,
            "audit_outcome": outcome,
            "completed_at": completed_at,
        },
    )
    row = connection.execute(
        text(
            """
            SELECT review_decision_id, decision, reviewer_actor_id,
                   reviewer_role_code, remarks, decided_at_utc
            FROM auditcore.review_decisions
            WHERE tenant_id = :tenant_id AND review_decision_id = :decision_id
            """
        ),
        {"tenant_id": tenant_id, "decision_id": decision_id},
    ).mappings().one()
    return _decision_response(row)
