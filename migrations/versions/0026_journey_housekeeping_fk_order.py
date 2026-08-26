"""Fix Journey housekeeping ordering for add-ons that reference Evidence.

Revision ID: 0026_journey_housekeeping_fk
Revises: 0025_journey_housekeeping
Create Date: 2026-08-26
"""
from alembic import op

revision = "0026_journey_housekeeping_fk"
down_revision = "0025_journey_housekeeping"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # 0025 performs the main privileged purge. Journey add-ons also reference
    # Evidence, so remove that child table (and its audit chain) first, then invoke
    # the 0025 function for the remainder of the transaction graph.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.hard_delete_journey_transactions_v2(
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
                ALTER TABLE auditcore.audit_events DISABLE TRIGGER USER;
                DELETE FROM auditcore.audit_events
                WHERE tenant_id=p_tenant_id
                  AND entity_id IN (
                        SELECT journey_addon_id::text
                        FROM auditcore.journey_addons
                        WHERE tenant_id=p_tenant_id
                          AND journey_id = ANY(p_journey_ids)
                  );
                ALTER TABLE auditcore.audit_events ENABLE TRIGGER USER;

                DELETE FROM auditcore.audit_chain_heads
                WHERE tenant_id=p_tenant_id
                  AND entity_id IN (
                        SELECT journey_addon_id::text
                        FROM auditcore.journey_addons
                        WHERE tenant_id=p_tenant_id
                          AND journey_id = ANY(p_journey_ids)
                  );

                DELETE FROM auditcore.journey_addons
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);
            END IF;

            SELECT auditcore.hard_delete_journey_transactions(
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
        "auditcore.hard_delete_journey_transactions_v2(varchar, uuid[]) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION "
        f"auditcore.hard_delete_journey_transactions_v2(varchar, uuid[]) TO {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "auditcore.hard_delete_journey_transactions_v2(varchar, uuid[])"
    )
