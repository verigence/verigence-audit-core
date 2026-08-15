from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError, NotFoundError


def create_workflow_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    workflow_type: str,
    process_area: str,
    task_type: str,
    assigned_role_code: str | None = None,
    assigned_actor_id: str | None = None,
    dealer_id: UUID | None = None,
    outlet_id: UUID | None = None,
    task_payload: dict[str, Any] | None = None,
    effect_key: str | None = None,
    correlation_id: str | None = None,
) -> UUID:
    workflow_instance_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.workflow_instances (
                tenant_id, journey_id, workflow_type, correlation_id
            ) VALUES (
                :tenant_id, :journey_id, :workflow_type, :correlation_id
            ) RETURNING workflow_instance_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "workflow_type": workflow_type,
            "correlation_id": correlation_id,
        },
    ).scalar_one()
    task_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.workflow_tasks (
                tenant_id, workflow_instance_id, journey_id,
                process_area, task_type, assigned_role_code,
                assigned_actor_id, dealer_id, outlet_id,
                task_payload, effect_key, correlation_id
            ) VALUES (
                :tenant_id, :workflow_instance_id, :journey_id,
                :process_area, :task_type, :assigned_role_code,
                :assigned_actor_id, :dealer_id, :outlet_id,
                CAST(:task_payload AS jsonb), :effect_key, :correlation_id
            ) RETURNING workflow_task_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "workflow_instance_id": workflow_instance_id,
            "journey_id": journey_id,
            "process_area": process_area,
            "task_type": task_type,
            "assigned_role_code": assigned_role_code,
            "assigned_actor_id": assigned_actor_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "task_payload": json.dumps(task_payload or {}),
            "effect_key": effect_key,
            "correlation_id": correlation_id,
        },
    ).scalar_one()
    _append_task_event(
        connection,
        tenant_id=tenant_id,
        task_id=task_id,
        workflow_instance_id=workflow_instance_id,
        journey_id=journey_id,
        event_type="CREATED",
        from_status=None,
        to_status="READY",
        actor_id=None,
        reason=None,
        correlation_id=correlation_id,
    )
    return task_id


def get_workflow_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
):
    row = connection.execute(
        text(
            """
            SELECT wt.workflow_task_id, wt.workflow_instance_id, wt.journey_id,
                   wt.process_area, wt.task_type, wt.task_status,
                   wt.assigned_role_code, wt.assigned_actor_id,
                   wt.dealer_id, wt.outlet_id, wt.task_payload,
                   wt.effect_key, wt.correlation_id,
                   wt.claimed_at_utc, wt.started_at_utc,
                   wt.completed_at_utc, wt.cancelled_at_utc,
                   wi.workflow_type, wi.workflow_status
            FROM auditcore.workflow_tasks wt
            JOIN auditcore.workflow_instances wi
              ON wi.tenant_id = wt.tenant_id
             AND wi.workflow_instance_id = wt.workflow_instance_id
            WHERE wt.tenant_id = :tenant_id
              AND wt.workflow_task_id = :task_id
            """
        ),
        {"tenant_id": tenant_id, "task_id": workflow_task_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-015",
            title="Workflow task not found",
            detail="Workflow task not found for the requested tenant.",
        )
    return row


def claim_workflow_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    actor_id: str,
) -> None:
    _transition_task(
        connection,
        tenant_id=tenant_id,
        workflow_task_id=workflow_task_id,
        expected_status="READY",
        next_status="CLAIMED",
        event_type="CLAIMED",
        actor_id=actor_id,
        extra_sql="assigned_actor_id = :actor_id, claimed_at_utc = now(),",
    )


def start_workflow_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    actor_id: str,
) -> None:
    _transition_task(
        connection,
        tenant_id=tenant_id,
        workflow_task_id=workflow_task_id,
        expected_status="CLAIMED",
        next_status="IN_PROGRESS",
        event_type="STARTED",
        actor_id=actor_id,
        extra_sql="started_at_utc = now(),",
    )


def complete_workflow_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    actor_id: str,
) -> None:
    _transition_task(
        connection,
        tenant_id=tenant_id,
        workflow_task_id=workflow_task_id,
        expected_status="IN_PROGRESS",
        next_status="COMPLETED",
        event_type="COMPLETED",
        actor_id=actor_id,
        extra_sql="completed_at_utc = now(),",
    )


def cancel_workflow_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    actor_id: str,
    reason: str,
) -> None:
    task = get_workflow_task(
        connection,
        tenant_id=tenant_id,
        workflow_task_id=workflow_task_id,
    )
    if task["task_status"] not in {
        "PENDING",
        "READY",
        "CLAIMED",
        "IN_PROGRESS",
        "RETRY_WAIT",
    }:
        raise _transition_error(task["task_status"], "CANCELLED")
    _transition_task(
        connection,
        tenant_id=tenant_id,
        workflow_task_id=workflow_task_id,
        expected_status=task["task_status"],
        next_status="CANCELLED",
        event_type="CANCELLED",
        actor_id=actor_id,
        reason=reason,
        extra_sql=(
            "cancelled_at_utc = now(), cancelled_by_actor_id = :actor_id, "
            "cancel_reason = :reason,"
        ),
    )


def _transition_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    expected_status: str,
    next_status: str,
    event_type: str,
    actor_id: str,
    extra_sql: str = "",
    reason: str | None = None,
) -> None:
    row = connection.execute(
        text(
            f"""
            UPDATE auditcore.workflow_tasks
            SET {extra_sql}
                task_status = :next_status,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id
              AND workflow_task_id = :task_id
              AND task_status = :expected_status
            RETURNING workflow_instance_id, journey_id, correlation_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "expected_status": expected_status,
            "next_status": next_status,
            "actor_id": actor_id,
            "reason": reason,
        },
    ).mappings().one_or_none()
    if row is None:
        task = get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=workflow_task_id,
        )
        raise _transition_error(task["task_status"], next_status)
    _append_task_event(
        connection,
        tenant_id=tenant_id,
        task_id=workflow_task_id,
        workflow_instance_id=row["workflow_instance_id"],
        journey_id=row["journey_id"],
        event_type=event_type,
        from_status=expected_status,
        to_status=next_status,
        actor_id=actor_id,
        reason=reason,
        correlation_id=row["correlation_id"],
    )


def _append_task_event(
    connection: Connection,
    *,
    tenant_id: str,
    task_id: UUID,
    workflow_instance_id: UUID,
    journey_id: UUID,
    event_type: str,
    from_status: str | None,
    to_status: str,
    actor_id: str | None,
    reason: str | None,
    correlation_id: str | None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.workflow_task_events (
                tenant_id, workflow_task_id, workflow_instance_id,
                journey_id, event_type, from_status, to_status,
                actor_id, actor_type, reason, correlation_id
            ) VALUES (
                :tenant_id, :task_id, :workflow_instance_id,
                :journey_id, :event_type, :from_status, :to_status,
                :actor_id, :actor_type, :reason, :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "workflow_instance_id": workflow_instance_id,
            "journey_id": journey_id,
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "actor_id": actor_id,
            "actor_type": "USER" if actor_id else "SYSTEM",
            "reason": reason,
            "correlation_id": correlation_id,
        },
    )


def _transition_error(current_status: str, requested_status: str) -> AuditCoreError:
    return AuditCoreError(
        error_code="VAC-CONFLICT-002",
        status_code=409,
        title="Invalid workflow task transition",
        detail=f"Cannot transition workflow task from {current_status} to {requested_status}.",
    )
