"""0174_activity_source -- time on Verigence from a phone's browser is its own source (2026-10-27).

Product owner: "Employee engagement -- are we capturing from mobile as well? Add
the source being used." The Android / iOS app already reported as ANDROID / IOS;
a phone's browser reported as WEB and, being a second tally for the same day,
was merged with the desktop's by GREATEST (not added). MOBILE_WEB keeps it apart.

Revision chain: after 0173 (the finance / insurance rules PR); merge that first.
"""
from __future__ import annotations

from alembic import op

revision = "0174_activity_source"
down_revision = "0173_dealer_insurance_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_user_activity_daily DROP CONSTRAINT IF EXISTS p2_user_activity_daily_client_kind_check;
        ALTER TABLE auditcore.p2_user_activity_daily ADD CONSTRAINT p2_user_activity_daily_client_kind_check
          CHECK (client_kind IN ('WEB', 'MOBILE_WEB', 'ANDROID', 'IOS'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_user_activity_daily DROP CONSTRAINT IF EXISTS p2_user_activity_daily_client_kind_check;
        ALTER TABLE auditcore.p2_user_activity_daily ADD CONSTRAINT p2_user_activity_daily_client_kind_check
          CHECK (client_kind IN ('WEB', 'ANDROID', 'IOS')) NOT VALID;
        """
    )
