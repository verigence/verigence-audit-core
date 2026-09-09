"""UC03 Booking confirmation: intimation date, minimum-payment confirm, tenant rule config.

Revision ID: 0072_uc03_booking_confirmation
Revises: 0071_journey_housekeeping_bank
Create Date: 2026-09-09

Three data-driven Booking checks, independent of the PC's own Submit/Review
workflow:

  1. On the Booking Form's own confirmation, record its printed booking_date
     as the Booking's Intimation Date.
  2. On every payment receipt's confirmation, walk all durable Booking
     payments in receipt-date order, accumulate the running total, and stamp
     Booking Confirmed (its own date + timestamp) the moment that total
     reaches the tenant's minimum booking amount -- deliberately NOT stored
     in business_status, which the PC's own Submit action already owns and
     unconditionally overwrites; a second, uncoordinated writer on that same
     column would silently stomp one or the other.
  3. Corporate-discount / exchange-bonus / scrappage-bonus-without-evidence
     and below-minimum-payment findings both need a per-tenant configurable
     minimum booking amount -- tenant_rule_config holds it as a plain,
     directly-editable row (not a versioned/published discount policy; this
     is an operational threshold, not a discount rule).
"""
from alembic import op

revision = "0072_uc03_booking_confirmation"
down_revision = "0071_journey_housekeeping_bank"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_DEFAULT_MINIMUM_BOOKING_AMOUNT = "11000"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.journey_stage_states
            ADD COLUMN intimation_date date,
            ADD COLUMN booking_confirm_date date,
            ADD COLUMN booking_confirmed_at_utc timestamptz
        """
    )

    op.execute(
        f"""
        CREATE TABLE auditcore.tenant_rule_config (
            tenant_id               varchar(128) NOT NULL
                                    REFERENCES auditcore.projects(tenant_id),
            minimum_booking_amount  numeric(18,2) NOT NULL
                                    DEFAULT {_DEFAULT_MINIMUM_BOOKING_AMOUNT}
                                    CHECK (minimum_booking_amount >= 0),
            updated_by_actor_id     varchar(160),
            created_at_utc          timestamptz NOT NULL DEFAULT now(),
            updated_at_utc          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id)
        )
        """
    )
    op.execute(
        "ALTER TABLE auditcore.tenant_rule_config ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE auditcore.tenant_rule_config FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation_tenant_rule_config
        ON auditcore.tenant_rule_config
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tenant_rule_config_updated
        BEFORE UPDATE ON auditcore.tenant_rule_config
        FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.tenant_rule_config TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE DELETE ON auditcore.tenant_rule_config FROM {_RUNTIME_ROLE}"
    )
    op.execute(
        """
        COMMENT ON TABLE auditcore.tenant_rule_config IS
        'Per-tenant, directly-editable operational thresholds for UC03 checkpoint
        rules (e.g. minimum booking amount) -- not a versioned/published policy.'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.tenant_rule_config")
    op.execute(
        """
        ALTER TABLE auditcore.journey_stage_states
            DROP COLUMN IF EXISTS intimation_date,
            DROP COLUMN IF EXISTS booking_confirm_date,
            DROP COLUMN IF EXISTS booking_confirmed_at_utc
        """
    )
