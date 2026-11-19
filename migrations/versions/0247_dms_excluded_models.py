"""0247_dms_excluded_models -- a DMS dump row of a model out of scope is EXCLUDED (product
owner 2026-11-19: ALFA LOAD, ALFA PASSENGER, JEETO STRONG, TREO, TREO AUTO, TREO PLUS, UDO,
ZEO, ZOR GRAND "are out of scope for us.. so dont show them or remove them from the DMS dumps
for mahindra whenever gets uploaded"). The row stays in the file as uploaded; it is never
matched, counted, shown or reported. Rows already uploaded are set at their next re-match.
"""
from __future__ import annotations

from alembic import op

revision = "0247_dms_excluded_models"
down_revision = "0246_task_switches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows DROP CONSTRAINT IF EXISTS p2_dms_dump_rows_outcome_check")
    op.execute(
        """
        ALTER TABLE auditcore.p2_dms_dump_rows ADD CONSTRAINT p2_dms_dump_rows_outcome_check
        CHECK (outcome IN ('MATCHED', 'NOT_IN_TOOL', 'MISSING', 'CANCELLED', 'OUT_OF_PERIOD', 'EXCLUDED'))
        """
    )


def downgrade() -> None:
    op.execute("UPDATE auditcore.p2_dms_dump_rows SET outcome = 'OUT_OF_PERIOD' WHERE outcome = 'EXCLUDED'")
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows DROP CONSTRAINT IF EXISTS p2_dms_dump_rows_outcome_check")
    op.execute(
        """
        ALTER TABLE auditcore.p2_dms_dump_rows ADD CONSTRAINT p2_dms_dump_rows_outcome_check
        CHECK (outcome IN ('MATCHED', 'NOT_IN_TOOL', 'MISSING', 'CANCELLED', 'OUT_OF_PERIOD'))
        """
    )
