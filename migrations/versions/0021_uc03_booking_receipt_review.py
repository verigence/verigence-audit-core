"""Persist reviewed UC03 Booking payment-receipt fields in the Payment domain."""
from alembic import op

revision = "0021_uc03_receipt_review"
down_revision = "0020_uc03_part1_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.payments
            ADD COLUMN receipt_number varchar(160),
            ADD COLUMN receipt_date date,
            ADD COLUMN receipt_details jsonb NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute(
        """
        CREATE INDEX ix_payments_source_evidence
        ON auditcore.payments(tenant_id, journey_id, source_evidence_id)
        WHERE source_evidence_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_payments_source_evidence")
    op.execute(
        """
        ALTER TABLE auditcore.payments
            DROP COLUMN IF EXISTS receipt_details,
            DROP COLUMN IF EXISTS receipt_date,
            DROP COLUMN IF EXISTS receipt_number
        """
    )
