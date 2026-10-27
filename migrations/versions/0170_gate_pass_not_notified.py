"""0170_gate_pass_not_notified -- how many deliveries were not notified to the PC (2026-10-27).

When the gate pass lists more deliveries than the tool shows, the PC now says how
many the dealership did not notify to them (a count, not yes/no). The rest are
deliveries the PC has not uploaded:

    not uploaded by PC = deliveries on the gate pass - (found in the tool + not notified)
"""
from __future__ import annotations

from alembic import op

revision = "0170_gate_pass_not_notified"
down_revision = "0169_people_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_gate_passes
          ADD COLUMN IF NOT EXISTS not_notified_count integer
            CHECK (not_notified_count IS NULL OR not_notified_count >= 0);
        -- Answers given before the count existed: NO meant all of the gap was not notified.
        UPDATE auditcore.p2_gate_passes
           SET not_notified_count = CASE WHEN files_held IS FALSE
                                         THEN GREATEST(COALESCE(new_deliveries, 0) - COALESCE(delivery_files, 0), 0)
                                         ELSE 0 END
         WHERE divergence = 'MISSING_FILES' AND files_held IS NOT NULL AND not_notified_count IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_gate_passes DROP COLUMN IF EXISTS not_notified_count")
