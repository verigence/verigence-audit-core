"""0165_activity_sessions -- sessions on Verigence per person and day
(product owner, 2026-10-27: how many times someone used the system and the
shortest, average and longest stretch, per day).

Additive: ``p2_user_activity_daily.sessions`` holds the day's sessions as the
browser counted them, ``[{"start": ISO, "end": ISO, "activeSeconds": n}]``.
A session is a stretch of use with no break of 30 minutes or more.
"""
from __future__ import annotations

from alembic import op

revision = "0165_activity_sessions"
down_revision = "0164_task_manager"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_user_activity_daily
          ADD COLUMN IF NOT EXISTS sessions jsonb NOT NULL DEFAULT '[]'::jsonb;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_user_activity_daily DROP COLUMN IF EXISTS sessions")
