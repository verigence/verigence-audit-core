"""0234_engagement_output_window -- productive time and "clicking around" (2026-11-16).

Product owner, 16 Nov 2026: someone who keeps clicking tabs counts as active the whole
time, so employee engagement shows, beside active time:

- productive minutes: active minutes followed by output (a file, a saved value, a task
  action, a review) within the output window, 20 minutes by default;
- "active, no output": the rest of the active minutes;
- "clicking around": a 20-minute block with no output and more than N screen changes,
  10 by default.

Both numbers are SuperAdmin settings, for every Project or for one Project, in the same
rows as the reading window (0204): ``p2_activity_settings`` gains
``output_window_minutes`` (5-60) and ``hop_screen_changes`` (3-50); NULL follows the
all-Projects value, and no value anywhere means the default. The change log records the
new values beside the reading window, with ``setting`` = 'ENGAGEMENT' on those rows
(NULL: a reading-window change, as before).

The all-Projects row must carry a reading window (0204); a row written only for these two
settings carries the current window, so that rule still holds.
"""
from __future__ import annotations

from alembic import op

revision = "0234_engagement_output_window"
down_revision = "0233_notification_seen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("p2_activity_settings", "p2_activity_setting_changes"):
        op.execute(
            f"ALTER TABLE auditcore.{table} ADD COLUMN IF NOT EXISTS output_window_minutes smallint "
            "CHECK (output_window_minutes BETWEEN 5 AND 60)"
        )
        op.execute(
            f"ALTER TABLE auditcore.{table} ADD COLUMN IF NOT EXISTS hop_screen_changes smallint "
            "CHECK (hop_screen_changes BETWEEN 3 AND 50)"
        )
    op.execute(
        "ALTER TABLE auditcore.p2_activity_setting_changes ADD COLUMN IF NOT EXISTS setting varchar(20) "
        "CHECK (setting IS NULL OR setting = 'ENGAGEMENT')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_activity_setting_changes DROP COLUMN IF EXISTS setting")
    for table in ("p2_activity_settings", "p2_activity_setting_changes"):
        op.execute(f"ALTER TABLE auditcore.{table} DROP COLUMN IF EXISTS hop_screen_changes")
        op.execute(f"ALTER TABLE auditcore.{table} DROP COLUMN IF EXISTS output_window_minutes")
