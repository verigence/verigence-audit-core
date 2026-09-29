"""Extend Journey housekeeping to the four Phase 2 tables it could not delete.

Revision ID: 0133_journey_housekeeping_p2
Revises: 0132_merge_0131_heads
Create Date: 2026-09-29

Same gap migrations 0045/0071/0093/0107 each fixed for their own era's new
tables. Four tables added since 0107 hold a foreign key with no cascade to a
table the hard-delete function removes, so a purge of a Journey that has any
of their rows stops with a foreign key violation (a 409 to the caller):

  - ``p2_upload_batches`` (``replaces_evidence_id`` -> evidence): set when a
    document is replaced by a new scan. Evidence is deleted before the Journey.
  - ``rule_executions`` (``audit_finding_id`` -> audit_findings): the log of
    every rule run, pointing at the finding it raised.
  - ``delivery_vehicle_photos`` (-> journeys): the vehicle pictures.
  - ``journey_delivery_vin_observation_proposals`` (-> journeys and
    workflow_tasks): a VIN entered by the PC on a task.

Reproduced on a real schema: each one alone makes the purge fail. The other
Phase 2 tables (p2_tasks, p2_work_queue and the rest) cascade from the Journey
and needed nothing.

Same wrapper pattern as 0107: pre-delete only the new child rows, then
delegate to the previously tested function.
"""
from alembic import op

revision = "0133_journey_housekeeping_p2"
down_revision = "0132_merge_0131_heads"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_PREVIOUS_FUNCTION = "hard_delete_journey_transactions_pre_0133"


def upgrade() -> None:
    op.execute(
        "ALTER FUNCTION auditcore.hard_delete_journey_transactions(varchar, uuid[]) "
        f"RENAME TO {_PREVIOUS_FUNCTION}"
    )
    op.execute(
        f"""
        CREATE FUNCTION auditcore.hard_delete_journey_transactions(
            p_tenant_id varchar,
            p_journey_ids uuid[]
        ) RETURNS jsonb
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, auditcore
        AS $$
        DECLARE
            v_receipt jsonb;
        BEGIN
            IF p_tenant_id IS NULL OR btrim(p_tenant_id) = '' THEN
                RAISE EXCEPTION 'TENANT_ID_REQUIRED' USING ERRCODE='invalid_parameter_value';
            END IF;

            IF COALESCE(cardinality(p_journey_ids), 0) > 0 THEN
                -- References evidence (a replaced document). Its page queue
                -- cascades from it.
                DELETE FROM auditcore.p2_upload_batches
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- References audit findings.
                DELETE FROM auditcore.rule_executions
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- References the Journey.
                DELETE FROM auditcore.delivery_vehicle_photos
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- References the Journey and workflow tasks.
                DELETE FROM auditcore.journey_delivery_vin_observation_proposals
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);
            END IF;

            SELECT auditcore.{_PREVIOUS_FUNCTION}(
                p_tenant_id,
                p_journey_ids
            ) INTO v_receipt;
            RETURN v_receipt;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "auditcore.hard_delete_journey_transactions(varchar, uuid[]) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION "
        f"auditcore.hard_delete_journey_transactions(varchar, uuid[]) TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION "
        f"auditcore.{_PREVIOUS_FUNCTION}(varchar, uuid[]) FROM {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "auditcore.hard_delete_journey_transactions(varchar, uuid[])"
    )
    op.execute(
        f"ALTER FUNCTION auditcore.{_PREVIOUS_FUNCTION}(varchar, uuid[]) "
        "RENAME TO hard_delete_journey_transactions"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION "
        f"auditcore.hard_delete_journey_transactions(varchar, uuid[]) TO {_RUNTIME_ROLE}"
    )
