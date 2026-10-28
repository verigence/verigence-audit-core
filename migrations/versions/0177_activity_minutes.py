"""0177_activity_minutes -- each minute of the day and the screens used (2026-10-28).

Product owner: "what all different tabs have been clicked, what is the longest
period when no task was completed or file uploaded, how many 20-minute
stretches were spent active or idle". The day's counters alone cannot say
when, so the browser now also sends:

- ``minute_states``: one character per minute of the IST day (1440):
  ``.`` nothing, ``B`` app hidden, ``I`` idle, ``A`` active (a click, key or
  scroll in the last 4 minutes), ``K`` a click, tap or key in that very minute;
- ``screen_visits``: each screen / journey tab opened, with its time.

Only Superadmin and Executive see them (the engagement report).
"""
from __future__ import annotations

from alembic import op

revision = "0177_activity_minutes"
down_revision = "0176_retire_ndc_signature"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_user_activity_daily
          ADD COLUMN IF NOT EXISTS minute_states text,
          ADD COLUMN IF NOT EXISTS screen_visits jsonb NOT NULL DEFAULT '[]'::jsonb;
        ALTER TABLE auditcore.p2_user_activity_daily DROP CONSTRAINT IF EXISTS p2_user_activity_daily_minute_states_check;
        ALTER TABLE auditcore.p2_user_activity_daily ADD CONSTRAINT p2_user_activity_daily_minute_states_check
          CHECK (minute_states IS NULL OR (length(minute_states) = 1440 AND minute_states ~ '^[.BIAK]*$'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_user_activity_daily DROP CONSTRAINT IF EXISTS p2_user_activity_daily_minute_states_check;
        ALTER TABLE auditcore.p2_user_activity_daily DROP COLUMN IF EXISTS screen_visits;
        ALTER TABLE auditcore.p2_user_activity_daily DROP COLUMN IF EXISTS minute_states;
        """
    )
