"""0167_p2_worker_jobs -- one-off worker jobs without restarting the worker.

Restart review (2026-10-27): on 26 Oct the P2 worker was restarted 34 times;
10 of them only to start a one-off job (statement re-reads, diagnostics
reports), because a job was started by changing a variable on the worker and
Railway redeploys a service whenever a variable changes. Each restart left the
items the worker held locked for ten minutes. A job is now a row here: the
running worker picks it up within a minute and runs it beside its normal work,
so starting a job no longer restarts anything. The variables still work.

Platform-level (no tenant): a job may span every Project.
"""
from __future__ import annotations

from alembic import op

revision = "0167_p2_worker_jobs"
down_revision = "0166_gate_pass_photo_quality"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_worker_jobs (
          job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          job_kind varchar(40) NOT NULL,
          tag varchar(160) NOT NULL DEFAULT '',
          reports text[] NOT NULL DEFAULT '{}',
          status varchar(16) NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED')),
          requested_by varchar(160),
          requested_at_utc timestamptz NOT NULL DEFAULT now(),
          started_at_utc timestamptz,
          finished_at_utc timestamptz,
          worker_id varchar(160),
          detail text
        );
        CREATE INDEX IF NOT EXISTS p2_worker_jobs_pending
          ON auditcore.p2_worker_jobs (requested_at_utc) WHERE status = 'PENDING';
        GRANT SELECT, INSERT, UPDATE ON auditcore.p2_worker_jobs TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.p2_worker_jobs")
