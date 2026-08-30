"""Add Dealer scope to the effective-dated Discount & Policy Master.

Revision ID: 0041_uc02_dealer_policy
Revises: 0040_uc03_booking_fields
Create Date: 2026-08-30

Management Referral policy can vary by Dealer. Dealer-scoped policy rows therefore
reference the canonical tenant-owned Dealer identity instead of storing Dealer names
or codes as free text in the persisted master.
"""

from alembic import op

revision = "0041_uc02_dealer_policy"
down_revision = "0040_uc03_booking_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.discount_policy_parameters
            ADD COLUMN dealer_id uuid
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.discount_policy_parameters
            ADD CONSTRAINT fk_discount_policy_parameter_dealer
            FOREIGN KEY (tenant_id, dealer_id)
            REFERENCES auditcore.dealers(tenant_id, dealer_id)
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.discount_policy_parameters
            DROP CONSTRAINT IF EXISTS discount_policy_parameters_scope_type_check
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.discount_policy_parameters
            ADD CONSTRAINT ck_discount_policy_parameter_scope_type
            CHECK (scope_type IN (
                'PROJECT','DEALER','SEGMENT','MODEL','TRIM','CONFIGURATION'
            ))
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.discount_policy_parameters
            ADD CONSTRAINT ck_discount_policy_parameter_scope_reference
            CHECK (
                (scope_type = 'DEALER'
                    AND dealer_id IS NOT NULL
                    AND segment_id IS NULL
                    AND scope_key IS NULL)
                OR
                (scope_type <> 'DEALER' AND dealer_id IS NULL)
            )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_discount_policy_parameter_dealer
        ON auditcore.discount_policy_parameters(
            tenant_id, dealer_id, parameter_key
        )
        WHERE dealer_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_discount_policy_parameter_dealer")
    op.execute(
        """
        ALTER TABLE auditcore.discount_policy_parameters
            DROP CONSTRAINT IF EXISTS ck_discount_policy_parameter_scope_reference,
            DROP CONSTRAINT IF EXISTS ck_discount_policy_parameter_scope_type,
            DROP CONSTRAINT IF EXISTS fk_discount_policy_parameter_dealer
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.discount_policy_parameters
            ADD CONSTRAINT discount_policy_parameters_scope_type_check
            CHECK (scope_type IN (
                'PROJECT','SEGMENT','MODEL','TRIM','CONFIGURATION'
            ))
        """
    )
    op.execute(
        "ALTER TABLE auditcore.discount_policy_parameters DROP COLUMN IF EXISTS dealer_id"
    )
