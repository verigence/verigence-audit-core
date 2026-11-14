"""0225_worker_job_heartbeat -- a running one-off job proves it is alive (2026-11-14).

Product owner 2026-11-14: "why this bulk upload is stuck since yesterday" and "why no timeout
are defined for uploads in bulk and drive". A job runs on a thread of the P2 worker; a deploy
restarts the worker and the thread dies with it, but the job's row stayed RUNNING for ever, so
nothing ever picked it up again (a PC's drop at Aditya Motors - Cuttack stopped half read on 13
Nov, 09:32 UTC, when the worker was redeployed). A running job now beats a heartbeat; one whose
heartbeat stops, or that runs past its time limit, is taken again (a drop, a settle and a Drive
run are safe to run twice) or closed as interrupted (uc03_p2_worker).
"""
from __future__ import annotations

from alembic import op

revision = "0225_worker_job_heartbeat"
down_revision = "0224_pinned_model_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_worker_jobs ADD COLUMN IF NOT EXISTS heartbeat_at_utc timestamptz")
    op.execute("ALTER TABLE auditcore.p2_worker_jobs ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0")
    op.execute("CREATE INDEX IF NOT EXISTS p2_worker_jobs_running ON auditcore.p2_worker_jobs (status) "
               "WHERE status = 'RUNNING'")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.p2_worker_jobs_running")
    op.execute("ALTER TABLE auditcore.p2_worker_jobs DROP COLUMN IF EXISTS attempts")
    op.execute("ALTER TABLE auditcore.p2_worker_jobs DROP COLUMN IF EXISTS heartbeat_at_utc")
