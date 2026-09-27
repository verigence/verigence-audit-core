"""0124_uc03_p2_control_ledger — Audit Core owns control state for every executor.

p2_control_state becomes the authoritative ledger: one row per Journey
control with its mode, the stage it was last evaluated for, a human-readable
reason, result detail (e.g. compared values) and the linked finding. It is no
longer a copy of legacy rule_executions: the mirror trigger on that legacy
table is removed and P2 writes the ledger directly from each executor.

p2_control_units records each evaluation unit per Journey (native runner per
stage, Rule Engine per stage, derived P2 controls) with the fact fingerprint
it last evaluated, so unchanged facts are never re-evaluated.

Downgrade does not restore the legacy rule_executions mirror trigger: nothing
in P2 reads it any more.
"""
from __future__ import annotations

from alembic import op

revision = "0124_uc03_p2_control_ledger"
down_revision = "0123_uc03_p2_page_grouping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        DROP TRIGGER IF EXISTS trg_p2_rule_execution_mirror ON auditcore.rule_executions;
        DROP FUNCTION IF EXISTS auditcore.p2_mirror_rule_execution();

        ALTER TABLE auditcore.p2_control_state
          DROP CONSTRAINT IF EXISTS p2_control_state_executor_type_check,
          ADD CONSTRAINT p2_control_state_executor_type_check
            CHECK (executor_type IN ('NATIVE','RULE_ENGINE','EXTERNAL_RULE_ENGINE','P2')),
          ADD COLUMN control_mode varchar(20),
          ADD COLUMN stage_code varchar(20),
          ADD COLUMN details jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN evaluation_count integer NOT NULL DEFAULT 0,
          ADD COLUMN status_changed_at_utc timestamptz;

        CREATE INDEX IF NOT EXISTS ix_p2_control_state_status
          ON auditcore.p2_control_state(tenant_id, journey_id, control_status);

        CREATE TABLE auditcore.p2_control_units (
            tenant_id              varchar(128) NOT NULL,
            journey_id             uuid NOT NULL,
            unit_key               varchar(60) NOT NULL,
            unit_status            varchar(20) NOT NULL DEFAULT 'PENDING'
                                   CHECK (unit_status IN ('PENDING','OK','RETRY_PENDING','ERROR_TERMINAL')),
            evaluated_fingerprint  varchar(64),
            last_error             text,
            last_evaluated_at_utc  timestamptz,
            updated_at_utc         timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id, unit_key),
            FOREIGN KEY (tenant_id, journey_id)
              REFERENCES auditcore.journeys(tenant_id, journey_id) ON DELETE CASCADE
        );
        ALTER TABLE auditcore.p2_control_units ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_control_units FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_p2_control_units ON auditcore.p2_control_units
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());
        GRANT SELECT, INSERT, UPDATE ON auditcore.p2_control_units TO audit_core_runtime;
        REVOKE DELETE ON auditcore.p2_control_units FROM audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        DROP TABLE IF EXISTS auditcore.p2_control_units;
        DROP INDEX IF EXISTS auditcore.ix_p2_control_state_status;
        UPDATE auditcore.p2_control_state SET executor_type='RULE_ENGINE'
          WHERE executor_type='EXTERNAL_RULE_ENGINE';
        DELETE FROM auditcore.p2_control_state WHERE executor_type='P2';
        ALTER TABLE auditcore.p2_control_state
          DROP COLUMN IF EXISTS status_changed_at_utc,
          DROP COLUMN IF EXISTS evaluation_count,
          DROP COLUMN IF EXISTS details,
          DROP COLUMN IF EXISTS stage_code,
          DROP COLUMN IF EXISTS control_mode,
          DROP CONSTRAINT IF EXISTS p2_control_state_executor_type_check,
          ADD CONSTRAINT p2_control_state_executor_type_check
            CHECK (executor_type IN ('NATIVE','RULE_ENGINE'));
        """
    )
