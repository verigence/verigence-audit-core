"""0135_uc03_p2_insurance_source — insurance Inhouse or Self, as the PC confirms it.

Insurance is through the dealership (Inhouse) unless the customer arranged
their own (Self). Inhouse premium is part of the dealer's deal; Self is
not. The PC confirms which on the Deal tab or on the insurance-invoice
task (decision 2026-09-30); the cover-note rule that reads it from the
document follows. Phase 1's self_insurance_flag stays the source system's.
"""
from __future__ import annotations

from alembic import op

revision = "0135_uc03_p2_insurance_source"
down_revision = "0134_uc03_p2_other_document_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.insurance_records
            ADD COLUMN insurance_source varchar(16)
                CHECK (insurance_source IS NULL OR insurance_source IN ('INHOUSE','SELF')),
            ADD COLUMN insurance_source_set_by_actor_id varchar(128),
            ADD COLUMN insurance_source_set_at_utc timestamptz
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.insurance_records
            DROP COLUMN insurance_source,
            DROP COLUMN insurance_source_set_by_actor_id,
            DROP COLUMN insurance_source_set_at_utc
        """
    )
