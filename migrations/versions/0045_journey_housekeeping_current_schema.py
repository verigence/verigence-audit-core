"""Extend Journey housekeeping to all current UC03 transactional child tables.

Revision ID: 0045_journey_housekeeping
Revises: 0044_merge_uc02_policy_uc03
Create Date: 2026-08-30

The public hard-delete function predates the UC03 V2 review/capture tables added
in migrations 0031, 0036, 0037 and 0039. Those rows reference Journey and, for
some review rows, Evidence. A Super Admin purge therefore reached the older
parent delete while newer FK children were still present and PostgreSQL correctly
raised an IntegrityError.

Keep the established wrapper pattern from migration 0026: pre-delete only the
new child rows, then delegate to the previously tested housekeeping function.
This is deliberately additive and reversible; normal application DELETE rights
remain unchanged.
"""
from alembic import op

revision = "0045_journey_housekeeping"
down_revision = "0044_merge_uc02_policy_uc03"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_PREVIOUS_FUNCTION = "hard_delete_journey_transactions_pre_0045"


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
                -- Added in 0039. References Journey directly.
                DELETE FROM auditcore.journey_attribute_review_decisions
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- Added in 0037. References both Journey and Evidence, therefore
                -- it must be removed before the older function deletes Evidence.
                DELETE FROM auditcore.journey_attribute_resolutions
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- Added in 0031. Also references Journey and Evidence.
                DELETE FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- Added in 0036. Both capture tables reference Journey.
                DELETE FROM auditcore.document_capture_v2_documents
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                DELETE FROM auditcore.document_capture_v2_declarations
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
