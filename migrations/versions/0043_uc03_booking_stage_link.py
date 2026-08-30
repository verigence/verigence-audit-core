"""Ensure the UC03 BOOKING stage creates its Booking linkage row.

Revision ID: 0043_uc03_booking_stage_link
Revises: 0042_uc03_customer_relationship
Create Date: 2026-08-30

This is deliberately scoped to a BOOKING stage row rather than every generic
Journey, preventing unrelated Journey use cases from receiving a Booking record.
"""
from alembic import op

revision = "0043_uc03_booking_stage_link"
down_revision = "0042_uc03_customer_relationship"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.ensure_booking_for_uc03_stage()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.stage_code = 'BOOKING' THEN
                INSERT INTO auditcore.bookings (tenant_id, journey_id)
                VALUES (NEW.tenant_id, NEW.journey_id)
                ON CONFLICT (tenant_id, journey_id) DO NOTHING;
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_ensure_booking_for_uc03_stage
        AFTER INSERT OR UPDATE OF stage_code ON auditcore.journey_stage_states
        FOR EACH ROW
        WHEN (NEW.stage_code = 'BOOKING')
        EXECUTE FUNCTION auditcore.ensure_booking_for_uc03_stage();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ensure_booking_for_uc03_stage ON auditcore.journey_stage_states"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.ensure_booking_for_uc03_stage()")