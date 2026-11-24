"""0262_dms_dump_row_issues -- the rows a dealership is to correct in its DMS dump (2026-11-24).

Product owner, 24 Nov 2026: Utkal's delivery sheet came with its LOCATION column headed
"Delivery Date"; all 348 rows were taken without a date or an outlet. An upload whose date or
location cannot be read is now refused, naming the column (uc03_p2_dms_dumps.check_sheet); a
taken upload keeps the rows to correct in the sheet -- a date that is not one or has an
impossible year (left empty, never guessed), a booking after its delivery, a date after the
dump's date -- for the uploader and the Data uploads screen.
"""
from __future__ import annotations

from alembic import op

revision = "0262_dms_dump_row_issues"
down_revision = "0261_bulk_file_replaced"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_dms_dumps ADD COLUMN IF NOT EXISTS row_issues jsonb NOT NULL DEFAULT '[]'::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_dms_dumps DROP COLUMN IF EXISTS row_issues")
