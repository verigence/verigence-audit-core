"""0175_work_queue_order -- the worker takes work in the order it was last asked for (2026-10-28).

Product owner: "pls change the queue order". The worker took work oldest-created
first, and an item asked for again kept its first place: a Journey's checks,
re-asked 45 times in a day, always ran ahead of pages uploaded hours later, which
waited until the 30-minute clock failed them. queued_at_utc is when the item
last joined the line; the worker claims by it.

Existing rows: an item waiting now joined the line when it was last asked for
(updated_at_utc); any other row keeps its creation time.
"""
from __future__ import annotations

from alembic import op

revision = "0175_work_queue_order"
down_revision = "0174_activity_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_work_queue ADD COLUMN queued_at_utc timestamptz NOT NULL DEFAULT now();
        UPDATE auditcore.p2_work_queue
           SET queued_at_utc = CASE WHEN work_status IN ('PENDING','RETRY_WAIT') THEN updated_at_utc
                                    ELSE created_at_utc END;
        CREATE INDEX IF NOT EXISTS ix_p2_work_queue_tenant_queued
          ON auditcore.p2_work_queue(tenant_id, work_status, next_attempt_at_utc, queued_at_utc);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS auditcore.ix_p2_work_queue_tenant_queued;
        ALTER TABLE auditcore.p2_work_queue DROP COLUMN IF EXISTS queued_at_utc;
        """
    )
