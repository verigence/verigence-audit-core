"""0168_gate_pass_pages -- a gate pass of up to four photos, and Sunday and
Monday photographed together or apart (product owner 2026-10-27).

The gate-out register can run over several pages, so one gate pass now keeps
1 to 4 photos in ``p2_gate_pass_pages`` (page 1 stays on the gate pass row as
before). Monday's gate pass covers Sunday too: ``photo_mode`` says whether the
PC had one photo set for both days (COMBINED) or a set per day (SEPARATE), and
``day_counts`` keeps the per-day counts of a SEPARATE gate pass. Existing gate
passes are copied in as page 1.
"""
from __future__ import annotations

from alembic import op

revision = "0168_gate_pass_pages"
down_revision = "0167_p2_worker_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_gate_passes
          ADD COLUMN IF NOT EXISTS photo_mode varchar(16) NOT NULL DEFAULT 'COMBINED',
          ADD COLUMN IF NOT EXISTS day_counts jsonb;

        CREATE TABLE IF NOT EXISTS auditcore.p2_gate_pass_pages (
          tenant_id varchar(128) NOT NULL,
          gate_pass_id uuid NOT NULL,
          page_no smallint NOT NULL CHECK (page_no BETWEEN 1 AND 8),
          page_date date NOT NULL,
          object_key text NOT NULL,
          content_type varchar(64) NOT NULL,
          size_bytes integer NOT NULL,
          width_px integer,
          height_px integer,
          sharpness numeric(10, 2),
          brightness numeric(6, 2),
          quality_flag varchar(32),
          created_at_utc timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, gate_pass_id, page_no)
        );
        ALTER TABLE auditcore.p2_gate_pass_pages ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_gate_pass_pages FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation_p2_gate_pass_pages ON auditcore.p2_gate_pass_pages;
        CREATE POLICY tenant_isolation_p2_gate_pass_pages ON auditcore.p2_gate_pass_pages
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());
        GRANT SELECT, INSERT, UPDATE ON auditcore.p2_gate_pass_pages TO audit_core_runtime;

        INSERT INTO auditcore.p2_gate_pass_pages (tenant_id, gate_pass_id, page_no, page_date, object_key,
          content_type, size_bytes, width_px, height_px, sharpness, brightness, quality_flag, created_at_utc)
        SELECT tenant_id, gate_pass_id, 1, business_date, object_key, content_type, size_bytes, width_px,
               height_px, sharpness, brightness, quality_flag, created_at_utc
        FROM auditcore.p2_gate_passes
        ON CONFLICT DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.p2_gate_pass_pages")
    op.execute("ALTER TABLE auditcore.p2_gate_passes DROP COLUMN IF EXISTS day_counts, "
               "DROP COLUMN IF EXISTS photo_mode")
