from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.security import Principal
from audit_core.workflow import (
    cancel_workflow_task,
    claim_workflow_task,
    complete_workflow_task,
    get_workflow_task,
    start_workflow_task,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/tasks", tags=["audit-tasks"])


class TaskResponse(BaseModel):
    taskId: UUID
    taskType: str
    status: str
    dueAtUtc: datetime | None = None
    assignedActorId: str | None = None
    assignedRole: str | None = None


class TaskCancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


class TaskHistoryEventResponse(BaseModel):
    eventType: str
    fromStatus: str | None
    toStatus: str | None
    actorId: str | None
    actorType: str | None
    reason: str | None
    occurredAtUtc: datetime


def _response(row) -> TaskResponse:
    return TaskResponse(
        taskId=row["workflow_task_id"],
        taskType=row["task_type"],
        status=row["task_status"],
        dueAtUtc=row.get("due_at_utc"),
        assignedActorId=row["assigned_actor_id"],
        assignedRole=row["assigned_role_code"],
    )


def _task(
    connection: Connection,
    principal: Principal,
    *,
    tenant_id: str,
    task_id: UUID,
    permission: str,
):
    authorize(principal, tenant_id=tenant_id, permission=permission)
    set_tenant_context(connection, tenant_id)
    task = get_workflow_task(
        connection,
        tenant_id=tenant_id,
        workflow_task_id=task_id,
    )
    if task["dealer_id"] is not None:
        require_business_scope(
            connection,
            principal,
            tenant_id=tenant_id,
            dealer_id=task["dealer_id"],
            outlet_id=task["outlet_id"],
        )
    return task


@router.get("", response_model=list[TaskResponse])
def list_tasks(
    tenant_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[TaskResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.work.read")
    set_tenant_context(connection, tenant_id)
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT wt.workflow_task_id, wt.task_type, wt.task_status,
                   wt.due_at_utc, wt.assigned_actor_id, wt.assigned_role_code,
                   wt.dealer_id, wt.outlet_id
            FROM auditcore.workflow_tasks wt
            JOIN auditcore.business_assignments ba
              ON ba.tenant_id = wt.tenant_id
             AND ba.security_actor_id = :actor_id
             AND ba.assignment_status = 'ACTIVE'
             AND ba.effective_from <= now()
             AND (ba.effective_to IS NULL OR ba.effective_to >= now())
             AND (
                    ba.dealer_id IS NULL
                    OR (
                        ba.dealer_id = wt.dealer_id
                        AND (ba.outlet_id IS NULL OR ba.outlet_id = wt.outlet_id)
                    )
                 )
            WHERE wt.tenant_id = :tenant_id
            ORDER BY wt.due_at_utc NULLS LAST, wt.workflow_task_id
            """
        ),
        {"tenant_id": tenant_id, "actor_id": principal.subject},
    ).mappings().all()
    return [_response(row) for row in rows]


@router.get("/{task_id}", response_model=TaskResponse)
def read_task(
    tenant_id: str,
    task_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TaskResponse:
    return _response(
        _task(
            connection,
            principal,
            tenant_id=tenant_id,
            task_id=task_id,
            permission="audit.work.read",
        )
    )


@router.post("/{task_id}/claim", response_model=TaskResponse)
def claim_task(
    tenant_id: str,
    task_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TaskResponse:
    _task(
        connection,
        principal,
        tenant_id=tenant_id,
        task_id=task_id,
        permission="audit.work.update",
    )
    claim_workflow_task(
        connection,
        tenant_id=tenant_id,
        workflow_task_id=task_id,
        actor_id=principal.subject,
    )
    return _response(get_workflow_task(connection, tenant_id=tenant_id, workflow_task_id=task_id))


@router.post("/{task_id}/start", response_model=TaskResponse)
def start_task(
    tenant_id: str,
    task_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TaskResponse:
    _task(
        connection,
        principal,
        tenant_id=tenant_id,
        task_id=task_id,
        permission="audit.work.update",
    )
    start_workflow_task(
        connection,
        tenant_id=tenant_id,
        workflow_task_id=task_id,
        actor_id=principal.subject,
    )
    return _response(get_workflow_task(connection, tenant_id=tenant_id, workflow_task_id=task_id))


@router.post("/{task_id}/complete", response_model=TaskResponse)
def complete_task(
    tenant_id: str,
    task_id: UUID,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TaskResponse:
    _task(
        connection,
        principal,
        tenant_id=tenant_id,
        task_id=task_id,
        permission="audit.work.update",
    )

    def execute() -> dict:
        complete_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            actor_id=principal.subject,
        )
        response = _response(
            get_workflow_task(connection, tenant_id=tenant_id, workflow_task_id=task_id)
        )
        return response.model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"task.complete:{task_id}",
        idempotency_key=idempotency_key,
        request_payload={"taskId": str(task_id)},
        execute=execute,
        logical_result_id=str(task_id),
    )
    return TaskResponse.model_validate(body)


@router.post("/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(
    tenant_id: str,
    task_id: UUID,
    payload: TaskCancelInput,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TaskResponse:
    _task(
        connection,
        principal,
        tenant_id=tenant_id,
        task_id=task_id,
        permission="audit.work.manage",
    )

    def execute() -> dict:
        cancel_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            actor_id=principal.subject,
            reason=payload.reason,
        )
        response = _response(
            get_workflow_task(connection, tenant_id=tenant_id, workflow_task_id=task_id)
        )
        return response.model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"task.cancel:{task_id}",
        idempotency_key=idempotency_key,
        request_payload={"taskId": str(task_id), "reason": payload.reason},
        execute=execute,
        logical_result_id=str(task_id),
    )
    return TaskResponse.model_validate(body)


@router.get("/{task_id}/history", response_model=list[TaskHistoryEventResponse])
def task_history(
    tenant_id: str,
    task_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[TaskHistoryEventResponse]:
    _task(
        connection,
        principal,
        tenant_id=tenant_id,
        task_id=task_id,
        permission="audit.work.read",
    )
    rows = connection.execute(
        text(
            """
            SELECT event_type, from_status, to_status, actor_id,
                   actor_type, reason, occurred_at_utc
            FROM auditcore.workflow_task_events
            WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
            ORDER BY occurred_at_utc, workflow_task_event_id
            """
        ),
        {"tenant_id": tenant_id, "task_id": task_id},
    ).mappings().all()
    return [
        TaskHistoryEventResponse(
            eventType=row["event_type"],
            fromStatus=row["from_status"],
            toStatus=row["to_status"],
            actorId=row["actor_id"],
            actorType=row["actor_type"],
            reason=row["reason"],
            occurredAtUtc=row["occurred_at_utc"],
        )
        for row in rows
    ]
