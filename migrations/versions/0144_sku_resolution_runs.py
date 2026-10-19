"""0144_sku_resolution_runs — a kept summary of every "Run SKU Resolution" run.

The admin SKU Resolution run goes through a Project's Journeys in small batches. This keeps one row per
run (counts, who, when) and one row per Journey in it (what happened to it), so the result can be read
afterwards and not only while the page is open.
"""
from __future__ import annotations

from alembic import op

revision = "0144_sku_resolution_runs"
down_revision = "0143_reviewer_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.sku_resolution_runs (
          tenant_id varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
          run_id uuid NOT NULL DEFAULT gen_random_uuid(),
          status varchar(20) NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING','COMPLETE')),
          started_by_actor_id varchar(160),
          started_at_utc timestamptz NOT NULL DEFAULT now(),
          finished_at_utc timestamptz,
          journeys_total integer NOT NULL DEFAULT 0,
          journeys_processed integer NOT NULL DEFAULT 0,
          newly_matched integer NOT NULL DEFAULT 0,
          already_had_sku integer NOT NULL DEFAULT 0,
          still_unmatched integer NOT NULL DEFAULT 0,
          failed integer NOT NULL DEFAULT 0,
          with_sku_before integer NOT NULL DEFAULT 0,
          with_sku_after integer NOT NULL DEFAULT 0,
          PRIMARY KEY (tenant_id, run_id)
        );
        CREATE INDEX IF NOT EXISTS ix_sku_resolution_runs_recent
          ON auditcore.sku_resolution_runs (tenant_id, started_at_utc DESC);

        CREATE TABLE IF NOT EXISTS auditcore.sku_resolution_run_items (
          tenant_id varchar(128) NOT NULL,
          run_id uuid NOT NULL,
          journey_id uuid NOT NULL,
          journey_label text,
          model_label text,
          outcome varchar(20) NOT NULL
            CHECK (outcome IN ('NEWLY_MATCHED','ALREADY_HAD_SKU','STILL_UNMATCHED','FAILED')),
          sku_code varchar(120),
          detail text,
          recorded_at_utc timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, run_id, journey_id),
          FOREIGN KEY (tenant_id, run_id) REFERENCES auditcore.sku_resolution_runs(tenant_id, run_id)
            ON DELETE CASCADE
        );

        ALTER TABLE auditcore.sku_resolution_runs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.sku_resolution_runs FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS sku_resolution_runs_tenant ON auditcore.sku_resolution_runs;
        CREATE POLICY sku_resolution_runs_tenant ON auditcore.sku_resolution_runs
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());
        ALTER TABLE auditcore.sku_resolution_run_items ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.sku_resolution_run_items FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS sku_resolution_run_items_tenant ON auditcore.sku_resolution_run_items;
        CREATE POLICY sku_resolution_run_items_tenant ON auditcore.sku_resolution_run_items
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());

        GRANT SELECT, INSERT, UPDATE ON auditcore.sku_resolution_runs TO audit_core_runtime;
        GRANT SELECT, INSERT, UPDATE ON auditcore.sku_resolution_run_items TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.sku_resolution_run_items")
    op.execute("DROP TABLE IF EXISTS auditcore.sku_resolution_runs")
