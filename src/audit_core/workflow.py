from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError, NotFoundError

logger = structlog.get_logger(__name__)


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
    logger.info(
        "workflow_task_created",
        task_id=str(task_id),
        task_type=task_type,
        journey_id=str(journey_id),
        tenant_id=tenant_id,
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
                   wt.attempt_count, wt.max_attempts, wt.next_attempt_at_utc,
                   wt.lease_owner, wt.lease_acquired_at_utc,
                   wt.lease_heartbeat_at_utc, wt.lease_expires_at_utc,
                   wt.last_error_code, wt.last_error_summary,
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


def claim_worker_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    worker_id: str,
    lease_seconds: int = 60,
) -> int:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    row = connection.execute(
        text(
            """
            UPDATE auditcore.workflow_tasks
            SET task_status = 'CLAIMED',
                attempt_count = attempt_count + 1,
                claimed_at_utc = now(),
                lease_owner = :worker_id,
                lease_acquired_at_utc = now(),
                lease_heartbeat_at_utc = now(),
                lease_expires_at_utc = now() + (:lease_seconds * interval '1 second'),
                next_attempt_at_utc = NULL,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id
              AND workflow_task_id = :task_id
              AND available_at_utc <= now()
              AND task_status IN ('READY','RETRY_WAIT')
              AND (task_status = 'READY' OR next_attempt_at_utc IS NULL OR next_attempt_at_utc <= now())
              AND attempt_count < max_attempts
            RETURNING workflow_instance_id, journey_id, correlation_id, attempt_count
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        },
    ).mappings().one_or_none()
    if row is None:
        task = get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=workflow_task_id,
        )
        raise _transition_error(task["task_status"], "CLAIMED")

    connection.execute(
        text(
            """
            INSERT INTO auditcore.workflow_task_attempts (
                tenant_id, workflow_task_id, attempt_no,
                worker_id, started_at_utc, correlation_id
            ) VALUES (
                :tenant_id, :task_id, :attempt_no,
                :worker_id, now(), :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "attempt_no": row["attempt_count"],
            "worker_id": worker_id,
            "correlation_id": row["correlation_id"],
        },
    )
    _append_task_event(
        connection,
        tenant_id=tenant_id,
        task_id=workflow_task_id,
        workflow_instance_id=row["workflow_instance_id"],
        journey_id=row["journey_id"],
        event_type="WORKER_CLAIMED",
        from_status="READY",
        to_status="CLAIMED",
        actor_id=None,
        reason=None,
        correlation_id=row["correlation_id"],
    )
    return row["attempt_count"]


def start_worker_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    worker_id: str,
    lease_seconds: int = 60,
) -> None:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    row = connection.execute(
        text(
            """
            UPDATE auditcore.workflow_tasks
            SET task_status = 'IN_PROGRESS',
                started_at_utc = COALESCE(started_at_utc, now()),
                lease_heartbeat_at_utc = now(),
                lease_expires_at_utc = now() + (:lease_seconds * interval '1 second'),
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id
              AND workflow_task_id = :task_id
              AND task_status = 'CLAIMED'
              AND lease_owner = :worker_id
              AND lease_expires_at_utc > now()
            RETURNING workflow_instance_id, journey_id, correlation_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        },
    ).mappings().one_or_none()
    if row is None:
        task = get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=workflow_task_id,
        )
        raise _transition_error(task["task_status"], "IN_PROGRESS")
    _append_task_event(
        connection,
        tenant_id=tenant_id,
        task_id=workflow_task_id,
        workflow_instance_id=row["workflow_instance_id"],
        journey_id=row["journey_id"],
        event_type="WORKER_STARTED",
        from_status="CLAIMED",
        to_status="IN_PROGRESS",
        actor_id=None,
        reason=None,
        correlation_id=row["correlation_id"],
    )


def heartbeat_worker_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    worker_id: str,
    lease_seconds: int = 60,
) -> None:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    updated = connection.execute(
        text(
            """
            UPDATE auditcore.workflow_tasks
            SET lease_heartbeat_at_utc = now(),
                lease_expires_at_utc = now() + (:lease_seconds * interval '1 second'),
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id
              AND workflow_task_id = :task_id
              AND task_status IN ('CLAIMED','IN_PROGRESS')
              AND lease_owner = :worker_id
              AND lease_expires_at_utc > now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        },
    ).rowcount
    if updated != 1:
        task = get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=workflow_task_id,
        )
        raise _transition_error(task["task_status"], task["task_status"])


def schedule_worker_retry(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    worker_id: str,
    retry_after_seconds: int,
    error_code: str,
    error_summary: str,
) -> None:
    if retry_after_seconds < 0:
        raise ValueError("retry_after_seconds cannot be negative")
    row = connection.execute(
        text(
            """
            UPDATE auditcore.workflow_tasks
            SET task_status = 'RETRY_WAIT',
                next_attempt_at_utc = now() + (:retry_after_seconds * interval '1 second'),
                lease_owner = NULL,
                lease_acquired_at_utc = NULL,
                lease_heartbeat_at_utc = NULL,
                lease_expires_at_utc = NULL,
                last_error_code = :error_code,
                last_error_summary = :error_summary,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id
              AND workflow_task_id = :task_id
              AND task_status IN ('CLAIMED','IN_PROGRESS')
              AND lease_owner = :worker_id
              AND attempt_count < max_attempts
            RETURNING workflow_instance_id, journey_id, correlation_id, attempt_count
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "worker_id": worker_id,
            "retry_after_seconds": retry_after_seconds,
            "error_code": error_code,
            "error_summary": error_summary,
        },
    ).mappings().one_or_none()
    if row is None:
        task = get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=workflow_task_id,
        )
        raise _transition_error(task["task_status"], "RETRY_WAIT")
    connection.execute(
        text(
            """
            UPDATE auditcore.workflow_task_attempts
            SET ended_at_utc = now(),
                attempt_result = 'RETRYABLE_FAILURE',
                error_code = :error_code,
                error_summary = :error_summary,
                next_retry_at_utc = now() + (:retry_after_seconds * interval '1 second')
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
            "retry_after_seconds": retry_after_seconds,
            "error_code": error_code,
            "error_summary": error_summary,
        },
    )
    _append_task_event(
        connection,
        tenant_id=tenant_id,
        task_id=workflow_task_id,
        workflow_instance_id=row["workflow_instance_id"],
        journey_id=row["journey_id"],
        event_type="RETRY_SCHEDULED",
        from_status="IN_PROGRESS",
        to_status="RETRY_WAIT",
        actor_id=None,
        reason=error_code,
        correlation_id=row["correlation_id"],
    )


def recover_stale_worker_tasks(
    connection: Connection,
    *,
    tenant_id: str,
    limit: int = 100,
) -> list[UUID]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    rows = connection.execute(
        text(
            """
            WITH stale AS (
                SELECT workflow_task_id
                FROM auditcore.workflow_tasks
                WHERE tenant_id = :tenant_id
                  AND task_status IN ('CLAIMED','IN_PROGRESS')
                  AND lease_expires_at_utc IS NOT NULL
                  AND lease_expires_at_utc <= now()
                  AND attempt_count < max_attempts
                ORDER BY lease_expires_at_utc, workflow_task_id
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE auditcore.workflow_tasks wt
            SET task_status = 'READY',
                next_attempt_at_utc = NULL,
                lease_owner = NULL,
                lease_acquired_at_utc = NULL,
                lease_heartbeat_at_utc = NULL,
                lease_expires_at_utc = NULL,
                last_error_code = 'LEASE_LOST',
                last_error_summary = 'Worker lease expired before task completion',
                updated_at_utc = now(),
                version_no = version_no + 1
            FROM stale
            WHERE wt.tenant_id = :tenant_id
              AND wt.workflow_task_id = stale.workflow_task_id
            RETURNING wt.workflow_task_id, wt.workflow_instance_id,
                      wt.journey_id, wt.correlation_id, wt.attempt_count
            """
        ),
        {"tenant_id": tenant_id, "limit": limit},
    ).mappings().all()
    for row in rows:
        connection.execute(
            text(
                """
                UPDATE auditcore.workflow_task_attempts
                SET ended_at_utc = now(),
                    attempt_result = 'LEASE_LOST',
                    error_code = 'LEASE_LOST',
                    error_summary = 'Worker lease expired before task completion'
                WHERE tenant_id = :tenant_id
                  AND workflow_task_id = :task_id
                  AND attempt_no = :attempt_no
                  AND ended_at_utc IS NULL
                """
            ),
            {
                "tenant_id": tenant_id,
                "task_id": row["workflow_task_id"],
                "attempt_no": row["attempt_count"],
            },
        )
        _append_task_event(
            connection,
            tenant_id=tenant_id,
            task_id=row["workflow_task_id"],
            workflow_instance_id=row["workflow_instance_id"],
            journey_id=row["journey_id"],
            event_type="STALE_LEASE_RECOVERED",
            from_status="IN_PROGRESS",
            to_status="READY",
            actor_id=None,
            reason="LEASE_LOST",
            correlation_id=row["correlation_id"],
        )
        logger.warning(
            "workflow_task_stale_recovered",
            task_id=str(row["workflow_task_id"]),
            tenant_id=tenant_id,
        )
    return [row["workflow_task_id"] for row in rows]


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
    logger.info(
        "workflow_task_transition",
        task_id=str(workflow_task_id),
        from_status=expected_status,
        to_status=next_status,
        actor_id=actor_id,
        tenant_id=tenant_id,
    )
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
