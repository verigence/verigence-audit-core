"""0138_p2_management_referral — the Management Referral (MR) discount, held here.

MR is a discount type in the standard that no master and no document
carries: it is opted out by default and a Team Lead opts a journey in,
with the amount and the reason, for a special case (decision 2026-09-30;
the TL's process follows). One row per journey.
"""
from __future__ import annotations

from alembic import op

revision = "0138_p2_management_referral"
down_revision = "0137_dealer_discount_grid"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.p2_management_referrals (
            tenant_id        varchar(128) NOT NULL,
            journey_id       uuid         NOT NULL,
            opted            boolean      NOT NULL DEFAULT false,
            amount           numeric(18,2),
            reason           text,
            set_by_actor_id  varchar(160),
            set_by_role      varchar(20),
            set_at_utc       timestamptz  NOT NULL DEFAULT now(),
            updated_at_utc   timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id),
            FOREIGN KEY (tenant_id, journey_id) REFERENCES auditcore.journeys(tenant_id, journey_id)
              ON DELETE CASCADE,
            CHECK (NOT opted OR (amount IS NOT NULL AND amount > 0))
        )
        """
    )
    op.execute(
        "CREATE TRIGGER trg_p2_management_referrals_updated_at "
        "BEFORE UPDATE ON auditcore.p2_management_referrals "
        "FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()"
    )
    op.execute("ALTER TABLE auditcore.p2_management_referrals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditcore.p2_management_referrals FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation_p2_management_referrals ON auditcore.p2_management_referrals "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON auditcore.p2_management_referrals TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.p2_management_referrals")
