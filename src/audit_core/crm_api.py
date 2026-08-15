from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.crm import create_crm_interaction_with_task
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import NotFoundError
from audit_core.security import Principal

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/crm-interactions",
    tags=["crm"],
)


class CrmInteractionCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interactionType: str
    notes: str | None = None
    assignedActorId: str | None = None


class CrmInteractionResponse(BaseModel):
    crmInteractionId: UUID
    journeyId: UUID
    interactionType: str
    interactionStatus: str
    outcomeCode: str | None
    notes: str | None
    actorId: str | None
    attemptedAtUtc: datetime | None
    completedAtUtc: datetime | None
    workflowTaskId: UUID | None


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


def _response(row) -> CrmInteractionResponse:
    return CrmInteractionResponse(
        crmInteractionId=row["crm_interaction_id"],
        journeyId=row["journey_id"],
        interactionType=row["interaction_type"],
        interactionStatus=row["interaction_status"],
        outcomeCode=row["outcome_code"],
        notes=row["notes"],
        actorId=row["actor_id"],
        attemptedAtUtc=row["attempted_at_utc"],
        completedAtUtc=row["completed_at_utc"],
        workflowTaskId=row["workflow_task_id"],
    )


def _interaction(connection: Connection, *, tenant_id: str, interaction_id: UUID):
    return connection.execute(
        text(
            """
            SELECT crm_interaction_id, journey_id, interaction_type,
                   interaction_status, outcome_code, notes, actor_id,
                   attempted_at_utc, completed_at_utc, workflow_task_id
            FROM auditcore.crm_interactions
            WHERE tenant_id = :tenant_id AND crm_interaction_id = :interaction_id
            """
        ),
        {"tenant_id": tenant_id, "interaction_id": interaction_id},
    ).mappings().one()


@router.get("", response_model=list[CrmInteractionResponse])
def list_crm_interactions(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[CrmInteractionResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.crm.read")
    set_tenant_context(connection, tenant_id)
    _journey_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    rows = connection.execute(
        text(
            """
            SELECT crm_interaction_id, journey_id, interaction_type,
                   interaction_status, outcome_code, notes, actor_id,
                   attempted_at_utc, completed_at_utc, workflow_task_id
            FROM auditcore.crm_interactions
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY created_at_utc, crm_interaction_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [_response(row) for row in rows]


@router.post("", response_model=CrmInteractionResponse, status_code=201)
def create_crm_interaction(
    tenant_id: str,
    journey_id: UUID,
    payload: CrmInteractionCreateInput,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> CrmInteractionResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.crm.manage")
    set_tenant_context(connection, tenant_id)
    journey = _journey_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    interaction_id = create_crm_interaction_with_task(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        interaction_type=payload.interactionType,
        effect_key=f"crm:{journey_id}:{idempotency_key}",
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
        assigned_actor_id=payload.assignedActorId,
        notes=payload.notes,
    )
    return _response(_interaction(connection, tenant_id=tenant_id, interaction_id=interaction_id))
