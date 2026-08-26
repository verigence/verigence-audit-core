"""Add independent PC verification state for UC03 Booking review.

Revision ID: 0028_uc03_pc_verification
Revises: 0027_uc03_pc_direct_di_documents
Create Date: 2026-08-26
"""
from alembic import op

revision = "0028_uc03_pc_verification"
down_revision = "0027_uc03_pc_direct_di_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.journey_stage_states
        ADD COLUMN IF NOT EXISTS pc_verification_status varchar(20)
            CHECK (pc_verification_status IN ('PENDING','VERIFIED'))
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_journey_stage_states_pc_review_pending
        ON auditcore.journey_stage_states (tenant_id, latest_activity_at_utc DESC, journey_id)
        WHERE stage_code='BOOKING'
          AND capture_completed_at_utc IS NOT NULL
          AND pc_verification_status='PENDING'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_journey_stage_states_pc_review_pending")
    op.execute(
        "ALTER TABLE auditcore.journey_stage_states DROP COLUMN IF EXISTS pc_verification_status"
    )
