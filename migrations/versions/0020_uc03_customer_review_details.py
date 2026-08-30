from alembic import op

revision = "0020_uc03_customer_review_details"
down_revision = "0019_uc03_booking_audit_derived"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add only the reviewed customer attributes that Audit Core actually needs.
    # Columns are nullable so the migration is metadata-only and does not rewrite
    # existing customer rows.
    op.execute(
        """
        ALTER TABLE auditcore.customers
            ADD COLUMN IF NOT EXISTS mobile_number varchar(32),
            ADD COLUMN IF NOT EXISTS address_text text,
            ADD COLUMN IF NOT EXISTS relation_type_code varchar(20),
            ADD COLUMN IF NOT EXISTS relation_name varchar(240)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_customers_tenant_mobile_number
        ON auditcore.customers (tenant_id, mobile_number)
        WHERE mobile_number IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_customers_tenant_mobile_number")
    op.execute(
        """
        ALTER TABLE auditcore.customers
            DROP COLUMN IF EXISTS relation_name,
            DROP COLUMN IF EXISTS relation_type_code,
            DROP COLUMN IF EXISTS address_text,
            DROP COLUMN IF EXISTS mobile_number
        """
    )
