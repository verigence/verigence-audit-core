"""Add typed Audit Core owners for unambiguous Booking evidence fields.

Revision ID: 0040_uc03_booking_fields
Revises: 0039_uc03_review_decisions
Create Date: 2026-08-30

DI remains source of truth for raw extracted machine facts. These columns hold
only reviewed/accepted business values whose Audit Core ownership is explicit.
Commercial extraction values and PAN/Aadhaar relationship evidence are not copied
into these columns; they continue to be consumed through DI with reference-only
resolution provenance.
"""
from alembic import op

revision = "0040_uc03_booking_fields"
down_revision = "0039_uc03_review_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.bookings
            ADD COLUMN expected_delivery_text varchar(240),
            ADD COLUMN expected_delivery_date date
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.registration_records
            ADD COLUMN registration_by varchar(240)
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.insurance_records
            ADD COLUMN insurance_by varchar(240)
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN auditcore.bookings.expected_delivery_text IS
        'Reviewed Booking evidence value for a printed expected-delivery timeframe/text; raw machine extraction remains in DI.'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN auditcore.bookings.expected_delivery_date IS
        'Reviewed complete expected-delivery calendar date when explicitly present in Booking evidence; no date is inferred from a timeframe.'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN auditcore.registration_records.registration_by IS
        'Reviewed party/person text explicitly shown as responsible for registration in Booking evidence.'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN auditcore.insurance_records.insurance_by IS
        'Reviewed party/person text explicitly shown as arranging/providing insurance in Booking evidence.'
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.insurance_records DROP COLUMN IF EXISTS insurance_by")
    op.execute("ALTER TABLE auditcore.registration_records DROP COLUMN IF EXISTS registration_by")
    op.execute(
        """
        ALTER TABLE auditcore.bookings
            DROP COLUMN IF EXISTS expected_delivery_date,
            DROP COLUMN IF EXISTS expected_delivery_text
        """
    )
