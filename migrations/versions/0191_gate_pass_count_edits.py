"""0191_gate_pass_count_edits -- a PC corrects the gate register's counts on the day (2026-11-03).

Product owner, 2026-11-03: "give an edit option for the day when that gate
pass is uploaded, as PCs are making mistakes in reporting the numbers". The
counts of a gate register may be entered again by its PC until the end of the
IST day it was uploaded (uc03_p2_gate_pass.submit_counts); each correction
keeps what was there before, so the Team Lead can see what changed:

- counts_history        every earlier set of counts, oldest first, with who
                        changed it and when
- counts_edit_count     how many times the counts were corrected
- counts_edited_at_utc  the last correction

Additive; existing rows start with no history.
"""
from __future__ import annotations

from alembic import op

revision = "0191_gate_pass_count_edits"
down_revision = "0190_bulk_uploads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_gate_passes "
               "ADD COLUMN IF NOT EXISTS counts_history jsonb NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE auditcore.p2_gate_passes "
               "ADD COLUMN IF NOT EXISTS counts_edit_count integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE auditcore.p2_gate_passes ADD COLUMN IF NOT EXISTS counts_edited_at_utc timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_gate_passes DROP COLUMN IF EXISTS counts_edited_at_utc")
    op.execute("ALTER TABLE auditcore.p2_gate_passes DROP COLUMN IF EXISTS counts_edit_count")
    op.execute("ALTER TABLE auditcore.p2_gate_passes DROP COLUMN IF EXISTS counts_history")
