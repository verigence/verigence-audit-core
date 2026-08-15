from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import NotFoundError
from audit_core.escalations import create_escalation_with_task, get_escalation, resolve_escalation
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.security import Principal

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/escalations",
    tags=["escalations"],
)


class EscalationCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    escalationType: str
    summary: str
    severity: str = "MEDIUM"
    assignedRoleCode: str | None = None
    assignedActorId: str | None = None
    details: str | None = None


class EscalationUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    escalationId: UUID
    status: Literal["RESOLVED", "CLOSED"]
    resolutionNotes: str


class EscalationResponse(BaseModel):
    escalationId: UUID
    journeyId: UUID | None
    escalationType: str
    severity: str
    status: str
    assignedRoleCode: str | None
    assignedActorId: str | None
    summary: str
    details: str | None
    openedAtUtc: datetime
    resolvedAtUtc: datetime | None
    resolutionNotes: str | None
    workflowTaskId: UUID | None = None
    versionNo: int


def _journey_scope(
    connection: Connection,
    principal: Principal,
    *,
    tenant_id: str,
    journey_id: UUID,
):
    row = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id
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
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=row["dealer_id"],
        outlet_id=row["outlet_id"],
    )
    return row


def _task_for_escalation(
    connection: Connection,
    *,
    tenant_id: str,
    escalation_id: UUID,
) -> UUID | None:
    return connection.execute(
        text(
            """
            SELECT workflow_task_id
            FROM auditcore.workflow_tasks
            WHERE tenant_id = :tenant_id
              AND task_payload->>'escalationId' = :escalation_id
            ORDER BY created_at_utc, workflow_task_id
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "escalation_id": str(escalation_id)},
    ).scalar_one_or_none()


def _response(row, task_id: UUID | None = None) -> EscalationResponse:
    return EscalationResponse(
        escalationId=row["escalation_id"],
        journeyId=row["journey_id"],
        escalationType=row["escalation_type"],
        severity=row["severity"],
        status=row["escalation_status"],
        assignedRoleCode=row["assigned_role_code"],
        assignedActorId=row["assigned_actor_id"],
        summary=row["summary"],
        details=row["details"],
        openedAtUtc=row["opened_at_utc"],
        resolvedAtUtc=row["resolved_at_utc"],
        resolutionNotes=row["resolution_notes"],
        workflowTaskId=task_id,
        versionNo=row["version_no"],
    )


@router.get("", response_model=list[EscalationResponse])
def list_escalations(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[EscalationResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.escalation.read")
    set_tenant_context(connection, tenant_id)
    _journey_scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    rows = connection.execute(
        text(
            """
            SELECT escalation_id, journey_id, escalation_type, severity,
                   escalation_status, assigned_role_code, assigned_actor_id,
                   summary, details, opened_at_utc, resolved_at_utc,
                   resolution_notes, version_no
            FROM auditcore.escalations
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY opened_at_utc, escalation_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        _response(
            row,
            _task_for_escalation(
                connection,
                tenant_id=tenant_id,
                escalation_id=row["escalation_id"],
            ),
        )
        for row in rows
    ]


@router.post("", response_model=EscalationResponse, status_code=201)
def create_escalation(
    tenant_id: str,
    journey_id: UUID,
    payload: EscalationCreateInput,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> EscalationResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.escalation.manage")
    set_tenant_context(connection, tenant_id)
    _journey_scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)

    def execute() -> dict:
        escalation_id, task_id = create_escalation_with_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            escalation_type=payload.escalationType,
            summary=payload.summary,
            effect_key=f"escalation:{journey_id}:{idempotency_key}",
            severity=payload.severity,
            assigned_role_code=payload.assignedRoleCode,
            assigned_actor_id=payload.assignedActorId,
            details=payload.details,
            created_by_actor_id=principal.subject,
        )
        response = _response(
            get_escalation(
                connection,
                tenant_id=tenant_id,
                escalation_id=escalation_id,
            ),
            task_id,
        )
        return response.model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"escalation.create:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
        response_status=201,
    )
    return EscalationResponse.model_validate(body)


@router.patch("", response_model=EscalationResponse)
def update_escalation(
    tenant_id: str,
    journey_id: UUID,
    payload: EscalationUpdateInput,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> EscalationResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.escalation.manage")
    set_tenant_context(connection, tenant_id)
    _journey_scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    current = get_escalation(
        connection,
        tenant_id=tenant_id,
        escalation_id=payload.escalationId,
    )
    if current["journey_id"] != journey_id:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Escalation not found",
            detail="Escalation not found for the requested Journey.",
        )

    def execute() -> dict:
        resolve_escalation(
            connection,
            tenant_id=tenant_id,
            escalation_id=payload.escalationId,
            resolution_notes=payload.resolutionNotes,
            final_status=payload.status,
        )
        response = _response(
            get_escalation(
                connection,
                tenant_id=tenant_id,
                escalation_id=payload.escalationId,
            ),
            _task_for_escalation(
                connection,
                tenant_id=tenant_id,
                escalation_id=payload.escalationId,
            ),
        )
        return response.model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"escalation.update:{payload.escalationId}",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
        logical_result_id=str(payload.escalationId),
    )
    return EscalationResponse.model_validate(body)
