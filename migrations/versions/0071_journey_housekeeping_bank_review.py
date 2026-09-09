"""Extend Journey housekeeping to bank-reconciliation and review-value tables.

Revision ID: 0071_journey_housekeeping_bank
Revises: 0070_uc03_bank_statement_req
Create Date: 2026-09-09

The hard-delete function last updated in migration 0045 predates six FK-
referencing tables added since: booking_form_review_values,
customer_identity_review_values, dealer_receipt_review_values (0048),
invoice_review_values (0065), and bank_statement_lines, payment_bank_matches
(0066). Every one of these has REVOKE DELETE ... FROM audit_core_runtime --
deliberately deletable only through this SECURITY DEFINER function -- but
none of them were ever added to it. A Super Admin Journey purge hit this
live: deleting auditcore.payments raised a ForeignKeyViolation because
payment_bank_matches still referenced it (payment_bank_matches also
references bank_statement_lines, so both are pre-deleted here, in that
order, before delegating).

Same established wrapper pattern as migration 0045: pre-delete only the new
child rows, then delegate to the previously tested housekeeping function.
"""
from alembic import op

revision = "0071_journey_housekeeping_bank"
down_revision = "0070_uc03_bank_statement_req"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_PREVIOUS_FUNCTION = "hard_delete_journey_transactions_pre_0071"


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
                -- Added in 0066. References Journey, Payments and Bank
                -- Statement Lines -- must be removed before any of those three.
                DELETE FROM auditcore.payment_bank_matches
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- Added in 0066. References Journey and Evidence.
                DELETE FROM auditcore.bank_statement_lines
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- Added in 0065. References Journey and Evidence.
                DELETE FROM auditcore.invoice_review_values
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                -- Added in 0048. Each references Journey and Evidence
                -- (customer_identity_review_values also references Customers,
                -- deleted later by the delegated function -- fine, this row
                -- is the child, not a blocker to that later deletion).
                DELETE FROM auditcore.booking_form_review_values
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                DELETE FROM auditcore.customer_identity_review_values
                WHERE tenant_id=p_tenant_id
                  AND journey_id = ANY(p_journey_ids);

                DELETE FROM auditcore.dealer_receipt_review_values
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
