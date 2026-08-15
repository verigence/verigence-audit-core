from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import NotFoundError


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
    connection.execute(
        text(
            """
            INSERT INTO auditcore.workflow_task_events (
                tenant_id, workflow_task_id, workflow_instance_id,
                journey_id, event_type, to_status, correlation_id
            ) VALUES (
                :tenant_id, :task_id, :workflow_instance_id,
                :journey_id, 'CREATED', 'READY', :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": task_id,
            "workflow_instance_id": workflow_instance_id,
            "journey_id": journey_id,
            "correlation_id": correlation_id,
        },
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
