from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError
from audit_core.workflow import create_workflow_task, get_workflow_task, schedule_worker_retry


def create_workflow_task_once(
    connection: Connection,
    *,
    tenant_id: str,
    effect_key: str,
    journey_id: UUID,
    workflow_type: str,
    process_area: str,
    task_type: str,
    assigned_role_code: str | None = None,
    assigned_actor_id: str | None = None,
    dealer_id: UUID | None = None,
    outlet_id: UUID | None = None,
    task_payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> UUID:
    if not effect_key.strip():
        raise ValueError("effect_key is required")

    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"{tenant_id}:{effect_key}"},
    )
    existing = connection.execute(
        text(
            """
            SELECT workflow_task_id
            FROM auditcore.workflow_tasks
            WHERE tenant_id = :tenant_id AND effect_key = :effect_key
            """
        ),
        {"tenant_id": tenant_id, "effect_key": effect_key},
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    return create_workflow_task(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        workflow_type=workflow_type,
        process_area=process_area,
        task_type=task_type,
        assigned_role_code=assigned_role_code,
        assigned_actor_id=assigned_actor_id,
        dealer_id=dealer_id,
        outlet_id=outlet_id,
        task_payload=task_payload,
        effect_key=effect_key,
        correlation_id=correlation_id,
    )


def fail_worker_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    worker_id: str,
    retry_after_seconds: int,
    error_code: str,
    error_summary: str,
) -> str:
    if retry_after_seconds < 0:
        raise ValueError("retry_after_seconds cannot be negative")

    row = connection.execute(
        text(
            """
            SELECT workflow_instance_id, journey_id, task_status,
                   attempt_count, max_attempts, lease_owner, correlation_id
            FROM auditcore.workflow_tasks
            WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "task_id": workflow_task_id},
    ).mappings().one_or_none()
    if row is None:
        get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=workflow_task_id,
        )
        raise AssertionError("unreachable")
    if row["task_status"] not in {"CLAIMED", "IN_PROGRESS"} or row["lease_owner"] != worker_id:
        raise AuditCoreError(
            error_code="VAC-CONFLICT-002",
            status_code=409,
            title="Invalid workflow task transition",
            detail="Worker does not hold the active lease for this task.",
        )

    if row["attempt_count"] < row["max_attempts"]:
        schedule_worker_retry(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=workflow_task_id,
            worker_id=worker_id,
            retry_after_seconds=retry_after_seconds,
            error_code=error_code,
            error_summary=error_summary,
        )
        return "RETRY_WAIT"

    connection.execute(
        text(
            """
            UPDATE auditcore.workflow_tasks
            SET task_status = 'DEAD_LETTER',
                next_attempt_at_utc = NULL,
                lease_owner = NULL,
                lease_acquired_at_utc = NULL,
                lease_heartbeat_at_utc = NULL,
                lease_expires_at_utc = NULL,
                last_error_code = :error_code,
                last_error_summary = :error_summary,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "error_code": error_code,
            "error_summary": error_summary,
        },
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.workflow_task_attempts
            SET ended_at_utc = now(),
                attempt_result = 'RETRYABLE_FAILURE',
                error_code = :error_code,
                error_summary = :error_summary,
                next_retry_at_utc = NULL
            WHERE tenant_id = :tenant_id
              AND workflow_task_id = :task_id
              AND attempt_no = :attempt_no
              AND ended_at_utc IS NULL
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "attempt_no": row["attempt_count"],
            "error_code": error_code,
            "error_summary": error_summary,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.workflow_task_events (
                tenant_id, workflow_task_id, workflow_instance_id,
                journey_id, event_type, from_status, to_status,
                actor_type, reason, correlation_id
            ) VALUES (
                :tenant_id, :task_id, :workflow_instance_id,
                :journey_id, 'RETRIES_EXHAUSTED', :from_status, 'DEAD_LETTER',
                'SYSTEM', :reason, :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "workflow_instance_id": row["workflow_instance_id"],
            "journey_id": row["journey_id"],
            "from_status": row["task_status"],
            "reason": error_code,
            "correlation_id": row["correlation_id"],
        },
    )
    return "DEAD_LETTER"
