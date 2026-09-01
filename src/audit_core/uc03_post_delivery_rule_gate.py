from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.workflow import get_workflow_task
from audit_core.workflow_reliability import create_workflow_task_once

_WORKFLOW_TYPE = "UC03_POST_DELIVERY_AUDIT"
_TASK_TYPE = "UC03_POST_DELIVERY_RULE_RUN"
_PROCESS_AREA = "POST_DELIVERY"


def _effect_key(journey_id: UUID, finalization_version: int) -> str:
    if finalization_version <= 0:
        raise ValueError("finalization_version must be positive")
    return f"uc03.post-delivery-rule-run:{journey_id}:{finalization_version}"


def ensure_post_delivery_rule_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    finalization_version: int,
    correlation_id: str | None,
) -> UUID:
    """Create or reuse one rule task for the committed finalization version."""

    journey = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id
            FROM auditcore.journeys
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    return UUID(
        str(
            create_workflow_task_once(
                connection,
                tenant_id=tenant_id,
                effect_key=_effect_key(journey_id, finalization_version),
                journey_id=journey_id,
                workflow_type=_WORKFLOW_TYPE,
                process_area=_PROCESS_AREA,
                task_type=_TASK_TYPE,
                dealer_id=journey["dealer_id"],
                outlet_id=journey["outlet_id"],
                task_payload={
                    "journeyId": str(journey_id),
                    "finalizationVersion": finalization_version,
                },
                correlation_id=correlation_id,
            )
        )
    )


def post_delivery_rule_gate_status(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    """Return the persisted rule-task/audit state and final-report readiness."""

    stage = connection.execute(
        text(
            """
            SELECT audit_state, audit_status, version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='POST_DELIVERY'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    task = connection.execute(
        text(
            """
            SELECT workflow_task_id, task_status, effect_key
            FROM auditcore.workflow_tasks
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND process_area='POST_DELIVERY'
              AND task_type='UC03_POST_DELIVERY_RULE_RUN'
            ORDER BY created_at_utc DESC, workflow_task_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    audit_state = str(stage["audit_state"]) if stage is not None else "NOT_STARTED"
    audit_status = str(stage["audit_status"]) if stage is not None else "NOT_EVALUATED"
    task_status = str(task["task_status"]) if task is not None else None

    return {
        "postDeliveryAuditState": audit_state,
        "postDeliveryAuditStatus": audit_status,
        "postDeliveryVersion": int(stage["version_no"]) if stage is not None else 0,
        "ruleTaskId": UUID(str(task["workflow_task_id"])) if task is not None else None,
        "ruleTaskStatus": task_status,
        "ruleTaskEffectKey": str(task["effect_key"]) if task is not None else None,
        "reportReady": task_status == "COMPLETED" and audit_state == "COMPLETE",
    }


def _complete_worker_task(
    connection: Connection,
    *,
    tenant_id: str,
    workflow_task_id: UUID,
    worker_id: str,
) -> bool:
    """Complete the leased rule task using existing workflow persistence semantics."""

    row = connection.execute(
        text(
            """
            UPDATE auditcore.workflow_tasks
            SET task_status='COMPLETED',
                completed_at_utc=now(),
                lease_owner=NULL,
                lease_acquired_at_utc=NULL,
                lease_heartbeat_at_utc=NULL,
                lease_expires_at_utc=NULL,
                next_attempt_at_utc=NULL,
                last_error_code=NULL,
                last_error_summary=NULL,
                updated_at_utc=now(),
                version_no=version_no+1
            WHERE tenant_id=:tenant_id
              AND workflow_task_id=:task_id
              AND task_status='IN_PROGRESS'
              AND lease_owner=:worker_id
              AND lease_expires_at_utc > now()
            RETURNING workflow_instance_id, journey_id,
                      correlation_id, attempt_count
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "worker_id": worker_id,
        },
    ).mappings().one_or_none()
    if row is None:
        return False

    connection.execute(
        text(
            """
            UPDATE auditcore.workflow_task_attempts
            SET ended_at_utc=now(), attempt_result='SUCCEEDED',
                error_code=NULL, error_summary=NULL, next_retry_at_utc=NULL
            WHERE tenant_id=:tenant_id
              AND workflow_task_id=:task_id
              AND attempt_no=:attempt_no
              AND ended_at_utc IS NULL
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "attempt_no": row["attempt_count"],
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.workflow_task_events (
                tenant_id, workflow_task_id, workflow_instance_id,
                journey_id, event_type, from_status, to_status,
                actor_type, correlation_id
            ) VALUES (
                :tenant_id, :task_id, :workflow_instance_id,
                :journey_id, 'WORKER_COMPLETED', 'IN_PROGRESS', 'COMPLETED',
                'SYSTEM', :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "task_id": workflow_task_id,
            "workflow_instance_id": row["workflow_instance_id"],
            "journey_id": row["journey_id"],
            "correlation_id": row["correlation_id"],
        },
    )
    return True


def _has_post_delivery_findings(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> bool:
    """Scope findings through their persisted POST_DELIVERY audit evaluation."""

    return bool(
        connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM auditcore.audit_findings f
                    JOIN auditcore.audit_evaluations e
                      ON e.tenant_id=f.tenant_id
                     AND e.audit_evaluation_id=f.audit_evaluation_id
                    WHERE f.tenant_id=:tenant_id
                      AND f.journey_id=:journey_id
                      AND e.process_area='POST_DELIVERY'
                      AND f.finding_status <> 'VOIDED'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
    )


def complete_post_delivery_rule_gate(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    workflow_task_id: UUID,
    worker_id: str,
) -> None:
    """Finish the post-Delivery boundary after rule results are durably persisted.

    This does not evaluate rules. A later approved worker calls it only after its
    audit evaluations/findings have committed in this transaction. The helper
    completes the leased workflow task and then marks POST_DELIVERY audit complete.
    """

    task = get_workflow_task(
        connection,
        tenant_id=tenant_id,
        workflow_task_id=workflow_task_id,
    )
    if UUID(str(task["journey_id"])) != journey_id:
        raise ValueError("Post-Delivery rule task does not belong to this Journey")
    if str(task["process_area"]) != _PROCESS_AREA or str(task["task_type"]) != _TASK_TYPE:
        raise ValueError("Workflow task is not the UC03 post-Delivery rule task")

    task_status = str(task["task_status"])
    if task_status == "IN_PROGRESS":
        if not _complete_worker_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=workflow_task_id,
            worker_id=worker_id,
        ):
            raise ValueError("Post-Delivery rule worker does not hold a valid active lease")
    elif task_status != "COMPLETED":
        raise ValueError("Post-Delivery audit cannot complete before the rule task succeeds")

    audit_status = (
        "FLAGS_RAISED"
        if _has_post_delivery_findings(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        else "NO_FLAGS"
    )
    updated = connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET audit_state='COMPLETE',
                audit_status=:audit_status,
                latest_activity_at_utc=now(),
                updated_at_utc=now(),
                version_no=version_no+1
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='POST_DELIVERY'
              AND audit_state='IN_PROGRESS'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "audit_status": audit_status,
        },
    ).rowcount
    if updated == 1:
        return

    state = connection.execute(
        text(
            """
            SELECT audit_state
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='POST_DELIVERY'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    if state != "COMPLETE":
        raise ValueError("POST_DELIVERY audit stage is not available for completion")
