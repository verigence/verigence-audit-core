from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.workflow_reliability import create_workflow_task_once


def create_escalation_with_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    escalation_type: str,
    summary: str,
    effect_key: str,
    severity: str = "MEDIUM",
    assigned_role_code: str | None = None,
    assigned_actor_id: str | None = None,
    details: str | None = None,
    created_by_actor_id: str | None = None,
    correlation_id: str | None = None,
) -> tuple[UUID, UUID]:
    journey = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if journey is None:
        raise LookupError("Journey not found for escalation")

    escalation_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.escalations (
                tenant_id, journey_id, escalation_type, severity,
                assigned_role_code, assigned_actor_id, summary, details,
                created_by_actor_id
            ) VALUES (
                :tenant_id, :journey_id, :escalation_type, :severity,
                :assigned_role_code, :assigned_actor_id, :summary, :details,
                :created_by_actor_id
            )
            RETURNING escalation_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "escalation_type": escalation_type,
            "severity": severity,
            "assigned_role_code": assigned_role_code,
            "assigned_actor_id": assigned_actor_id,
            "summary": summary,
            "details": details,
            "created_by_actor_id": created_by_actor_id,
        },
    ).scalar_one()

    task_id = create_workflow_task_once(
        connection,
        tenant_id=tenant_id,
        effect_key=effect_key,
        journey_id=journey_id,
        workflow_type="ESCALATION",
        process_area="ESCALATION",
        task_type="ESCALATION_FOLLOW_UP",
        assigned_role_code=assigned_role_code,
        assigned_actor_id=assigned_actor_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
        task_payload={
            "escalationId": str(escalation_id),
            "escalationType": escalation_type,
            "severity": severity,
        },
        correlation_id=correlation_id,
    )
    return escalation_id, task_id


def resolve_escalation(
    connection: Connection,
    *,
    tenant_id: str,
    escalation_id: UUID,
    resolution_notes: str,
    final_status: str = "RESOLVED",
) -> None:
    if final_status not in {"RESOLVED", "CLOSED"}:
        raise ValueError("final_status must be RESOLVED or CLOSED")
    row = connection.execute(
        text(
            """
            UPDATE auditcore.escalations
            SET escalation_status = :final_status,
                resolved_at_utc = COALESCE(resolved_at_utc, now()),
                resolution_notes = :resolution_notes,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id
              AND escalation_id = :escalation_id
              AND escalation_status IN ('OPEN','ACKNOWLEDGED')
            RETURNING escalation_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "escalation_id": escalation_id,
            "final_status": final_status,
            "resolution_notes": resolution_notes,
        },
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("Escalation not found or already resolved")


def get_escalation(
    connection: Connection,
    *,
    tenant_id: str,
    escalation_id: UUID,
):
    row = connection.execute(
        text(
            """
            SELECT escalation_id, journey_id, escalation_type, severity,
                   escalation_status, assigned_role_code, assigned_actor_id,
                   summary, details, opened_at_utc, resolved_at_utc,
                   resolution_notes, created_by_actor_id, version_no
            FROM auditcore.escalations
            WHERE tenant_id = :tenant_id AND escalation_id = :escalation_id
            """
        ),
        {"tenant_id": tenant_id, "escalation_id": escalation_id},
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("Escalation not found")
    return row
