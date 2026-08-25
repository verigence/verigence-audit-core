"""Add UC03 three-screen Booking capture and evidence localization support.

Revision ID: 0023_uc03_booking_review_flow
Revises: 0022_uc03_part1_default_profile
Create Date: 2026-08-25
"""
from alembic import op

revision = "0023_uc03_booking_review_flow"
down_revision = "0022_uc03_part1_default_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Explicit Booking-domain values entered by the Process Consultant on the
    # Booking Details screen.  These are operational facts, not copies of DI
    # extraction output.
    op.execute(
        """
        ALTER TABLE auditcore.bookings
            ADD COLUMN price_list_id uuid,
            ADD COLUMN outright_purchase boolean,
            ADD COLUMN accessories_taken boolean,
            ADD COLUMN fasttag_taken boolean,
            ADD COLUMN green_tax boolean,
            ADD COLUMN other_charges numeric(18,2),
            ADD COLUMN hp_charges numeric(18,2),
            ADD COLUMN exchange_discount_taken boolean,
            ADD COLUMN corporate_customer boolean,
            ADD COLUMN corporate_discount_taken boolean,
            ADD COLUMN corporate_discount_type varchar(120),
            ADD COLUMN corporate_id_available boolean,
            ADD COLUMN gst_benefit boolean
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.bookings
        ADD CONSTRAINT fk_bookings_price_list
        FOREIGN KEY (tenant_id, price_list_id)
        REFERENCES auditcore.price_lists(tenant_id, price_list_id)
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.bookings
        ADD CONSTRAINT ck_bookings_other_charges_nonnegative
        CHECK (other_charges IS NULL OR other_charges >= 0)
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.bookings
        ADD CONSTRAINT ck_bookings_hp_charges_nonnegative
        CHECK (hp_charges IS NULL OR hp_charges >= 0)
        """
    )

    # DI already returns source localisation.  Persist it on the Core evidence
    # fact snapshot so the authenticated review UI can highlight the source
    # without calling DI directly.
    op.execute(
        """
        ALTER TABLE auditcore.evidence_facts
            ADD COLUMN page_no integer,
            ADD COLUMN evidence_region jsonb
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.evidence_facts
        ADD CONSTRAINT ck_evidence_facts_page_no_positive
        CHECK (page_no IS NULL OR page_no > 0)
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.evidence_facts DROP CONSTRAINT IF EXISTS ck_evidence_facts_page_no_positive"
    )
    op.execute(
        "ALTER TABLE auditcore.evidence_facts DROP COLUMN IF EXISTS evidence_region, DROP COLUMN IF EXISTS page_no"
    )
    op.execute("ALTER TABLE auditcore.bookings DROP CONSTRAINT IF EXISTS ck_bookings_hp_charges_nonnegative")
    op.execute("ALTER TABLE auditcore.bookings DROP CONSTRAINT IF EXISTS ck_bookings_other_charges_nonnegative")
    op.execute("ALTER TABLE auditcore.bookings DROP CONSTRAINT IF EXISTS fk_bookings_price_list")
    op.execute(
        """
        ALTER TABLE auditcore.bookings
            DROP COLUMN IF EXISTS gst_benefit,
            DROP COLUMN IF EXISTS corporate_id_available,
            DROP COLUMN IF EXISTS corporate_discount_type,
            DROP COLUMN IF EXISTS corporate_discount_taken,
            DROP COLUMN IF EXISTS corporate_customer,
            DROP COLUMN IF EXISTS exchange_discount_taken,
            DROP COLUMN IF EXISTS hp_charges,
            DROP COLUMN IF EXISTS other_charges,
            DROP COLUMN IF EXISTS green_tax,
            DROP COLUMN IF EXISTS fasttag_taken,
            DROP COLUMN IF EXISTS accessories_taken,
            DROP COLUMN IF EXISTS outright_purchase,
            DROP COLUMN IF EXISTS price_list_id
        """
    )
