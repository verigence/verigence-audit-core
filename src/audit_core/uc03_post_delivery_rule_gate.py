from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.workflow import (
    complete_workflow_task,
    create_workflow_task,
    get_workflow_task,
)

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
    """Create or reuse the one post-Delivery rule task for a finalization version.

    The final-source command already holds the Journey aggregate advisory lock, so
    query-then-create is serialized for this Journey. The database's unique
    workflow-task effect-key index remains a second line of protection.
    """

    effect_key = _effect_key(journey_id, finalization_version)
    existing = connection.execute(
        text(
            """
            SELECT workflow_task_id
            FROM auditcore.workflow_tasks
            WHERE tenant_id=:tenant_id AND effect_key=:effect_key
            """
        ),
        {"tenant_id": tenant_id, "effect_key": effect_key},
    ).scalar_one_or_none()
    if existing is not None:
        return UUID(str(existing))

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

    return create_workflow_task(
        connection,
        tenant_id=tenant_id,
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
        effect_key=effect_key,
        correlation_id=correlation_id,
    )


def post_delivery_rule_gate_status(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    """Return aggregate rule-task/report-readiness state for one Journey."""

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
    audit_status = (
        str(stage["audit_status"]) if stage is not None else "NOT_EVALUATED"
    )
    task_status = str(task["task_status"]) if task is not None else None
    report_ready = task_status == "COMPLETED" and audit_state == "COMPLETE"

    return {
        "postDeliveryAuditState": audit_state,
        "postDeliveryAuditStatus": audit_status,
        "postDeliveryVersion": int(stage["version_no"]) if stage is not None else 0,
        "ruleTaskId": UUID(str(task["workflow_task_id"])) if task is not None else None,
        "ruleTaskStatus": task_status,
        "ruleTaskEffectKey": str(task["effect_key"]) if task is not None else None,
        "reportReady": report_ready,
    }


def complete_post_delivery_rule_gate(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    workflow_task_id: UUID,
    actor_id: str,
) -> None:
    """Complete the integration boundary after approved rule evaluation succeeds.

    Rule evaluators/results are intentionally out of scope. A later approved worker
    may call this only after it has persisted its evaluations/findings successfully.
    This helper completes the existing workflow task (if still IN_PROGRESS) and
    derives the aggregate audit status from persisted POST_DELIVERY findings.
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
        complete_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=workflow_task_id,
            actor_id=actor_id,
        )
    elif task_status != "COMPLETED":
        raise ValueError(
            "Post-Delivery audit cannot complete before the rule task succeeds"
        )

    has_flags = bool(
        connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM auditcore.audit_findings
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                      AND stage_code='POST_DELIVERY'
                      AND finding_status <> 'VOIDED'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
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
            "audit_status": "FLAGS_RAISED" if has_flags else "NO_FLAGS",
        },
    ).rowcount
    if updated != 1:
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
