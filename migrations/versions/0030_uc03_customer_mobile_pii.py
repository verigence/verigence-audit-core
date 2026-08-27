"""Persist full customer mobile while retaining last-four compatibility.

Revision ID: 0030_uc03_customer_mobile_pii
Revises: 0029_uc03_trade_in_status
Create Date: 2026-08-27
"""
from alembic import op

revision = "0030_uc03_customer_mobile_pii"
down_revision = "0029_uc03_trade_in_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.customers
        ADD COLUMN mobile_number varchar(24)
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.customers
        ADD CONSTRAINT ck_customers_mobile_number_normalized
        CHECK (
            mobile_number IS NULL
            OR mobile_number ~ '^\\+?[0-9]{4,20}$'
        )
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.customers
        ADD CONSTRAINT ck_customers_mobile_number_last4
        CHECK (
            mobile_number IS NULL
            OR mobile_last4 = right(regexp_replace(mobile_number, '[^0-9]', '', 'g'), 4)
        )
        """
    )
    op.execute(
        "COMMENT ON COLUMN auditcore.customers.mobile_number IS "
        "'Complete normalized customer mobile number. API disclosure is permission-controlled; DB value is not masked.'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.customers "
        "DROP CONSTRAINT IF EXISTS ck_customers_mobile_number_last4"
    )
    op.execute(
        "ALTER TABLE auditcore.customers "
        "DROP CONSTRAINT IF EXISTS ck_customers_mobile_number_normalized"
    )
    op.execute("ALTER TABLE auditcore.customers DROP COLUMN IF EXISTS mobile_number")
