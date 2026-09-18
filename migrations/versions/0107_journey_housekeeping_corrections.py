"""Extend Journey housekeeping to the three correction-proposal /
source-value tables added since migration 0093.

Revision ID: 0107_journey_housekeeping_corr
Revises: 0106_field_correction_task
Create Date: 2026-09-18

Same gap migrations 0045/0071/0093 each fixed for their own era's new
tables, recurring again: three tables added since 0093 all carry
``REVOKE DELETE ... FROM audit_core_runtime`` (deletable only through this
SECURITY DEFINER function) but were never added here.

Hit live: a Super Admin Journey purge raised
``ForeignKeyViolation: update or delete on table "audit_findings" violates
foreign key constraint "journey_document_field_correcti_tenant_id_audit_
finding_id..."`` because a ``journey_document_field_correction_proposals``
row (0090) still referenced it.

While fixing that, two more tables in the same "revoked from runtime,
purge-only" family were found with the identical gap and are fixed here
too, rather than one-at-a-time across three more live failures:
  - ``model_selection_correction_proposals`` (0102, reworked onto the Task
    Queue by 0103) references ``workflow_tasks`` and ``journeys`` -- both
    deleted by the base (0025) layer this delegates down to.
  - ``commercial_line_source_values`` (0101) references ``journeys`` and
    ``evidence`` -- also deleted by the base layer.

Same established wrapper pattern as migrations 0045/0071/0093: pre-delete
only the new child rows, then delegate to the previously tested
housekeeping function.
"""
from alembic import op

revision = "0107_journey_housekeeping_corr"
down_revision = "0106_field_correction_task"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_PREVIOUS_FUNCTION = "hard_delete_journey_transactions_pre_0107"


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
                -- Added in 0090. References Journey and Audit Findings --
                -- must be removed before the base layer deletes either.
                DELETE FROM auditcore.journey_document_field_correction_proposals
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- Added in 0102, reworked by 0103 onto the Task Queue.
                -- References Journey and Workflow Tasks -- must be removed
                -- before the base layer deletes either.
                DELETE FROM auditcore.model_selection_correction_proposals
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- Added in 0101. References Journey and Evidence.
                DELETE FROM auditcore.commercial_line_source_values
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
