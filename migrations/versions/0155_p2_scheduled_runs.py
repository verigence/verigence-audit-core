"""0155_p2_scheduled_runs — durable "already ran" markers for the P2 worker.

Retry policy review (2026-10-20): the nightly review and upload sweep was
remembered as "done for tonight" only in the worker's memory, so a worker
restart later the same night ran it again (and could tell a PC a page had
failed overnight before DI's night attempt had run). One row per tenant,
run name and key (the night's date, or "once" for a one-time deploy step)
is claimed with INSERT ... ON CONFLICT DO NOTHING inside the same
transaction as the work it guards, so a run happens once and a failed run
rolls its claim back. Additive: a new table, nothing else changes.
"""
from __future__ import annotations

from alembic import op

revision = "0155_p2_scheduled_runs"
down_revision = "0150_more_mahindra_model_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_scheduled_runs (
          tenant_id varchar(128) NOT NULL,
          run_name varchar(80) NOT NULL,
          run_key varchar(80) NOT NULL,
          claimed_by varchar(160),
          claimed_at_utc timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, run_name, run_key)
        );

        ALTER TABLE auditcore.p2_scheduled_runs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_scheduled_runs FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation_p2_scheduled_runs ON auditcore.p2_scheduled_runs;
        CREATE POLICY tenant_isolation_p2_scheduled_runs ON auditcore.p2_scheduled_runs
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());

        GRANT SELECT, INSERT ON auditcore.p2_scheduled_runs TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.p2_scheduled_runs")
