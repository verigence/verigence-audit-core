from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.workflow_reliability import create_workflow_task_once


def create_crm_interaction_with_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    interaction_type: str,
    effect_key: str,
    dealer_id: UUID | None = None,
    outlet_id: UUID | None = None,
    assigned_role_code: str = "CRM",
    assigned_actor_id: str | None = None,
    notes: str | None = None,
    correlation_id: str | None = None,
) -> UUID:
    task_id = create_workflow_task_once(
        connection,
        tenant_id=tenant_id,
        effect_key=effect_key,
        journey_id=journey_id,
        workflow_type="CRM_FOLLOW_UP",
        process_area="CRM",
        task_type="CRM_CALL",
        assigned_role_code=assigned_role_code,
        assigned_actor_id=assigned_actor_id,
        dealer_id=dealer_id,
        outlet_id=outlet_id,
        task_payload={"interactionType": interaction_type},
        correlation_id=correlation_id,
    )

    existing = connection.execute(
        text(
            """
            SELECT crm_interaction_id
            FROM auditcore.crm_interactions
            WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
            """
        ),
        {"tenant_id": tenant_id, "task_id": task_id},
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    return connection.execute(
        text(
            """
            INSERT INTO auditcore.crm_interactions (
                tenant_id, journey_id, interaction_type, interaction_status,
                notes, workflow_task_id
            ) VALUES (
                :tenant_id, :journey_id, :interaction_type, 'PENDING',
                :notes, :task_id
            )
            RETURNING crm_interaction_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "interaction_type": interaction_type,
            "notes": notes,
            "task_id": task_id,
        },
    ).scalar_one()


def record_crm_outcome(
    connection: Connection,
    *,
    tenant_id: str,
    crm_interaction_id: UUID,
    actor_id: str,
    interaction_status: str,
    outcome_code: str | None = None,
    notes: str | None = None,
    completed: bool = True,
) -> None:
    row = connection.execute(
        text(
            """
            UPDATE auditcore.crm_interactions
            SET interaction_status = :interaction_status,
                outcome_code = :outcome_code,
                notes = COALESCE(:notes, notes),
                actor_id = :actor_id,
                attempted_at_utc = COALESCE(attempted_at_utc, now()),
                completed_at_utc = CASE WHEN :completed THEN now() ELSE completed_at_utc END,
                updated_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND crm_interaction_id = :crm_interaction_id
            RETURNING crm_interaction_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "crm_interaction_id": crm_interaction_id,
            "actor_id": actor_id,
            "interaction_status": interaction_status,
            "outcome_code": outcome_code,
            "notes": notes,
            "completed": completed,
        },
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("CRM interaction not found")


def get_crm_interaction(
    connection: Connection,
    *,
    tenant_id: str,
    crm_interaction_id: UUID,
):
    row = connection.execute(
        text(
            """
            SELECT crm_interaction_id, journey_id, interaction_type,
                   interaction_status, outcome_code, notes, actor_id,
                   attempted_at_utc, completed_at_utc, workflow_task_id
            FROM auditcore.crm_interactions
            WHERE tenant_id = :tenant_id
              AND crm_interaction_id = :crm_interaction_id
            """
        ),
        {"tenant_id": tenant_id, "crm_interaction_id": crm_interaction_id},
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("CRM interaction not found")
    return row
