"""Extend Journey housekeeping to scrappage_certificate_review_values.

Revision ID: 0093_housekeeping_scrappage
Revises: 0092_delivery_req_seed_fn
Create Date: 2026-09-14

Migration 0079 added ``scrappage_certificate_review_values`` (FK to both
Journey and Evidence, ``REVOKE DELETE ... FROM audit_core_runtime`` --
deletable only through this SECURITY DEFINER function) but never added it
here, following the exact same gap migration 0071 fixed for six earlier
tables. Hit live: a Journey purge raised
``ForeignKeyViolation: update or delete on table "evidence" violates
foreign key constraint "scrappage_certificate_review__tenant_id_source_
evidence_id_fkey"`` because ``evidence`` rows were deleted while a
Scrappage Certificate review-value row still referenced them.

Same established wrapper pattern as migrations 0045/0071: pre-delete only
the new child rows, then delegate to the previously tested housekeeping
function.
"""
from alembic import op

revision = "0093_housekeeping_scrappage"
down_revision = "0092_delivery_req_seed_fn"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_PREVIOUS_FUNCTION = "hard_delete_journey_transactions_pre_0093"


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
                -- Added in 0079. References Journey and Evidence.
                DELETE FROM auditcore.scrappage_certificate_review_values
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
