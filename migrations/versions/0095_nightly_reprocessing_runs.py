"""Nightly Reprocessing run log, for the PMO/TL status tile.

Revision ID: 0095_nightly_reprocessing_runs
Revises: 0094_fix_payment_receipt_label
Create Date: 2026-09-14

DI's Nightly Reprocessing job (verigence-di migration 0040) gives every
currently-FAILED Capture V2 document a bounded number of further extraction
attempts, once nightly. It reports each run here (POST /v1/internal/di/
nightly-reprocessing-runs) purely so PMO/TL can see that the batch is
actually running and roughly how much work it's doing -- this table is not
itself in the extraction pipeline; DI's own processing_jobs/backout_jobs
remain the source of truth for what happened to any one document.
"""
from alembic import op

revision = "0095_nightly_reprocessing_runs"
down_revision = "0094_fix_payment_receipt_label"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.nightly_reprocessing_runs (
            nightly_reprocessing_run_id  uuid NOT NULL DEFAULT gen_random_uuid(),
            ran_at_utc                   timestamptz NOT NULL,
            documents_queued             integer,
            error                        text,
            reported_at_utc              timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (nightly_reprocessing_run_id)
        );

        CREATE INDEX ix_nightly_reprocessing_runs_ran_at
            ON auditcore.nightly_reprocessing_runs (ran_at_utc DESC);

        GRANT SELECT, INSERT ON auditcore.nightly_reprocessing_runs
            TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.nightly_reprocessing_runs")
