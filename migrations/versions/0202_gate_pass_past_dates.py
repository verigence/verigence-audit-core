"""0202_gate_pass_past_dates -- gate registers of earlier days, and editing a register's photos (2026-11-07).

Product owner, 2026-11-07: the PC chooses the date a gate register is for; a
register can be uploaded for any day from today back to 7 days ago, and a
register already on file can have photos added, replaced or removed while its
date is within today back to 3 days ago (uc03_p2_gate_pass).

A removed photo is never deleted: the page row is kept with who removed it and
when, and is left out of the register from then on. A photo added later keeps
who added it. So a page's number is its id within the register, no longer its
position, and is no longer capped at 8 (a register still holds at most four
photos at a time).

- p2_gate_pass_pages.removed_at_utc / removed_by_actor_id   a removed photo
- p2_gate_pass_pages.added_by_actor_id                      a photo added later
- p2_gate_passes.pages_edit_count / pages_edited_at_utc     photo edits made

Additive; existing pages stay live.
"""
from __future__ import annotations

from alembic import op

revision = "0202_gate_pass_past_dates"
down_revision = "0201_data_archive_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_gate_pass_pages ADD COLUMN IF NOT EXISTS removed_at_utc timestamptz")
    op.execute("ALTER TABLE auditcore.p2_gate_pass_pages ADD COLUMN IF NOT EXISTS removed_by_actor_id varchar(128)")
    op.execute("ALTER TABLE auditcore.p2_gate_pass_pages ADD COLUMN IF NOT EXISTS added_by_actor_id varchar(128)")
    op.execute("ALTER TABLE auditcore.p2_gate_pass_pages DROP CONSTRAINT IF EXISTS p2_gate_pass_pages_page_no_check")
    op.execute("ALTER TABLE auditcore.p2_gate_pass_pages "
               "ADD CONSTRAINT p2_gate_pass_pages_page_no_check CHECK (page_no >= 1)")
    op.execute("ALTER TABLE auditcore.p2_gate_passes "
               "ADD COLUMN IF NOT EXISTS pages_edit_count integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE auditcore.p2_gate_passes ADD COLUMN IF NOT EXISTS pages_edited_at_utc timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_gate_passes DROP COLUMN IF EXISTS pages_edited_at_utc")
    op.execute("ALTER TABLE auditcore.p2_gate_passes DROP COLUMN IF EXISTS pages_edit_count")
    op.execute("ALTER TABLE auditcore.p2_gate_pass_pages DROP CONSTRAINT IF EXISTS p2_gate_pass_pages_page_no_check")
    op.execute("ALTER TABLE auditcore.p2_gate_pass_pages "
               "ADD CONSTRAINT p2_gate_pass_pages_page_no_check CHECK (page_no BETWEEN 1 AND 8) NOT VALID")
    op.execute("ALTER TABLE auditcore.p2_gate_pass_pages DROP COLUMN IF EXISTS added_by_actor_id")
    op.execute("ALTER TABLE auditcore.p2_gate_pass_pages DROP COLUMN IF EXISTS removed_by_actor_id")
    op.execute("ALTER TABLE auditcore.p2_gate_pass_pages DROP COLUMN IF EXISTS removed_at_utc")
