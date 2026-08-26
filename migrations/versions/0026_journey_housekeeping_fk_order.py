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
    # Preserve the public housekeeping function contract while inserting a small
    # privileged pre-delete step for Journey add-ons, which also reference Evidence.
    op.execute(
        "ALTER FUNCTION auditcore.hard_delete_journey_transactions(varchar, uuid[]) "
        "RENAME TO hard_delete_journey_transactions_legacy"
    )
    op.execute(
        r"""
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

            SELECT auditcore.hard_delete_journey_transactions_legacy(
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
        "REVOKE ALL ON FUNCTION "
        "auditcore.hard_delete_journey_transactions_legacy(varchar, uuid[]) "
        "FROM audit_core_runtime"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "auditcore.hard_delete_journey_transactions(varchar, uuid[])"
    )
    op.execute(
        "ALTER FUNCTION "
        "auditcore.hard_delete_journey_transactions_legacy(varchar, uuid[]) "
        "RENAME TO hard_delete_journey_transactions"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION "
        f"auditcore.hard_delete_journey_transactions(varchar, uuid[]) TO {_RUNTIME_ROLE}"
    )
