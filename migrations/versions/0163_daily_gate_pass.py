"""0163_daily_gate_pass -- the Daily Gate Pass and the Daily Task category
(product owner, 2026-10-26).

A PC photographs the outlet's hand-written gate pass each day, says how many
new deliveries, stock transfers and test drives it lists, and the count of new
deliveries is checked against the Delivery files uploaded that day for the
outlet. A difference the PC cannot explain becomes a Daily Task for the Team
Lead (a task with no Journey: ``p2_daily_tasks``).

Additive: two new tables, nothing existing is changed.
"""
from __future__ import annotations

from alembic import op

revision = "0163_daily_gate_pass"
down_revision = "0162_deal_source_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_gate_passes (
            tenant_id               varchar(128) NOT NULL,
            gate_pass_id            uuid NOT NULL DEFAULT gen_random_uuid(),
            dealer_id               uuid NOT NULL,
            outlet_id               uuid NOT NULL,
            outlet_name             varchar(200) NOT NULL,
            pc_actor_id             varchar(160) NOT NULL,
            pc_name                 varchar(200) NOT NULL,
            business_date           date NOT NULL,
            status                  varchar(20) NOT NULL DEFAULT 'ACTIVE'
                                    CHECK (status IN ('ACTIVE', 'SUPERSEDED')),
            object_key              text NOT NULL,
            content_type            varchar(60) NOT NULL,
            size_bytes              bigint NOT NULL CHECK (size_bytes > 0),
            width_px                integer NOT NULL,
            height_px               integer NOT NULL,
            sharpness               numeric(10, 2) NOT NULL,
            new_deliveries          integer CHECK (new_deliveries >= 0),
            stock_transfers         integer CHECK (stock_transfers >= 0),
            test_drives             integer CHECK (test_drives >= 0),
            other_count             integer CHECK (other_count >= 0),
            other_note              text,
            delivery_files          integer,
            divergence              varchar(20)
                                    CHECK (divergence IN ('NONE', 'EXTRA_FILES', 'MISSING_FILES')),
            past_delivery_note      text,
            files_held              boolean,
            counts_submitted_at_utc timestamptz,
            completed_at_utc        timestamptz,
            created_at_utc          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, gate_pass_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_p2_gate_passes_active_day
          ON auditcore.p2_gate_passes(tenant_id, outlet_id, business_date)
          WHERE status = 'ACTIVE';
        CREATE INDEX IF NOT EXISTS ix_p2_gate_passes_date
          ON auditcore.p2_gate_passes(tenant_id, business_date, outlet_id);

        CREATE TABLE IF NOT EXISTS auditcore.p2_daily_tasks (
            tenant_id           varchar(128) NOT NULL,
            daily_task_id       uuid NOT NULL DEFAULT gen_random_uuid(),
            gate_pass_id        uuid,
            dealer_id           uuid NOT NULL,
            outlet_id           uuid NOT NULL,
            outlet_name         varchar(200) NOT NULL,
            business_date       date NOT NULL,
            category            varchar(40) NOT NULL DEFAULT 'DAILY_TASK',
            task_type           varchar(80) NOT NULL,
            title               varchar(300) NOT NULL,
            description         text NOT NULL,
            priority            varchar(20) NOT NULL DEFAULT 'HIGH'
                                CHECK (priority IN ('URGENT', 'HIGH', 'NORMAL', 'LOW')),
            assigned_role_code  varchar(40) NOT NULL DEFAULT 'TL',
            task_status         varchar(20) NOT NULL DEFAULT 'OPEN'
                                CHECK (task_status IN ('OPEN', 'DONE')),
            details             jsonb NOT NULL DEFAULT '{}'::jsonb,
            raised_by_actor_id  varchar(160),
            created_at_utc      timestamptz NOT NULL DEFAULT now(),
            resolved_at_utc     timestamptz,
            resolved_by_actor_id varchar(160),
            resolution_note     text,
            PRIMARY KEY (tenant_id, daily_task_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_p2_daily_tasks_gate_pass
          ON auditcore.p2_daily_tasks(tenant_id, gate_pass_id, task_type)
          WHERE gate_pass_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_p2_daily_tasks_open
          ON auditcore.p2_daily_tasks(tenant_id, task_status, created_at_utc DESC);

        ALTER TABLE auditcore.p2_gate_passes ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_gate_passes FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation_p2_gate_passes ON auditcore.p2_gate_passes;
        CREATE POLICY tenant_isolation_p2_gate_passes ON auditcore.p2_gate_passes
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());

        ALTER TABLE auditcore.p2_daily_tasks ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_daily_tasks FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation_p2_daily_tasks ON auditcore.p2_daily_tasks;
        CREATE POLICY tenant_isolation_p2_daily_tasks ON auditcore.p2_daily_tasks
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());

        GRANT SELECT, INSERT, UPDATE ON auditcore.p2_gate_passes, auditcore.p2_daily_tasks
          TO audit_core_runtime;
        REVOKE DELETE ON auditcore.p2_gate_passes, auditcore.p2_daily_tasks FROM audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS auditcore.p2_daily_tasks;
        DROP TABLE IF EXISTS auditcore.p2_gate_passes;
        """
    )
