"""0215_drive_schedule -- when the Drive files are taken each day, and for which outlets (product owner 2026-11-12).

"Time to run this daily should be configurable along with the dealership folders / or
outlets." Three settings on the Project's Drive connection (0214):

- daily_run_time     when the day's run starts, India time (21:00 to begin with).
- daily_run_enabled  off: nothing is taken on its own; only a PC's submit or an on-demand run.
- outlet_ids         the outlets that use Drive: only they get a folder, and only their files
                     are taken by the daily run. NULL: every outlet that has a PC.
"""
from __future__ import annotations

from alembic import op

revision = "0215_drive_schedule"
down_revision = "0214_drive_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.p2_drive_connections "
        "ADD COLUMN IF NOT EXISTS daily_run_time time NOT NULL DEFAULT '21:00', "
        "ADD COLUMN IF NOT EXISTS daily_run_enabled boolean NOT NULL DEFAULT true, "
        "ADD COLUMN IF NOT EXISTS outlet_ids uuid[]"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.p2_drive_connections DROP COLUMN IF EXISTS outlet_ids, "
        "DROP COLUMN IF EXISTS daily_run_enabled, DROP COLUMN IF EXISTS daily_run_time"
    )
