"""0173_dealer_insurance_codes -- the dealer's own insurance Agent / MISP codes (2026-10-27).

Product owner rule: insurance is in-house when the policy carries the dealer's
Agent code or MISP code, or the dealer's invoice bills the premium; otherwise
the PC confirms in-house or external on a task, writing the Agent / MISP code.
Audit Core holds no dealer insurance codes, so the codes PCs confirm are kept
here per dealer and recognised on every later policy.
"""
from __future__ import annotations

from alembic import op

revision = "0173_dealer_insurance_codes"
down_revision = "0172_finance_rule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.dealer_insurance_codes (
          tenant_id varchar(128) NOT NULL,
          dealer_id uuid NOT NULL,
          normalized_code varchar(120) NOT NULL,
          code_as_entered varchar(200) NOT NULL,
          confirmed_by_actor_id varchar(128),
          journey_id uuid,
          task_id uuid,
          created_at_utc timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, dealer_id, normalized_code),
          FOREIGN KEY (tenant_id, dealer_id) REFERENCES auditcore.dealers (tenant_id, dealer_id)
        );
        ALTER TABLE auditcore.dealer_insurance_codes ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.dealer_insurance_codes FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation_dealer_insurance_codes ON auditcore.dealer_insurance_codes;
        CREATE POLICY tenant_isolation_dealer_insurance_codes ON auditcore.dealer_insurance_codes
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());
        GRANT SELECT, INSERT, UPDATE ON auditcore.dealer_insurance_codes TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.dealer_insurance_codes")
