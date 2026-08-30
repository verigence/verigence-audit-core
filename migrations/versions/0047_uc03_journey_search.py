"""Add indexes for role-scoped UC03 Journey search.

Revision ID: 0047_uc03_journey_search
Revises: 0046_uc03_v2_review_mat
Create Date: 2026-08-30

Search remains a normal Audit Core read path. These indexes cover the human-facing
references used by PC/TL/PM search without introducing a denormalized search table
or a second authorization model.
"""

from alembic import op

revision = "0047_uc03_journey_search"
down_revision = "0046_uc03_v2_review_mat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_search_customer_display_name
            ON auditcore.customers (tenant_id, lower(display_name) text_pattern_ops);
        CREATE INDEX IF NOT EXISTS ix_uc03_search_customer_legal_name
            ON auditcore.customers (tenant_id, lower(legal_name) text_pattern_ops)
            WHERE legal_name IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_uc03_search_customer_mobile
            ON auditcore.customers (tenant_id, mobile_number)
            WHERE mobile_number IS NOT NULL;

        CREATE INDEX IF NOT EXISTS ix_uc03_search_booking_reference
            ON auditcore.bookings (tenant_id, upper(booking_reference) text_pattern_ops)
            WHERE booking_reference IS NOT NULL;

        CREATE INDEX IF NOT EXISTS ix_uc03_search_vehicle_vin
            ON auditcore.vehicle_records (tenant_id, upper(vin) text_pattern_ops)
            WHERE vin IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_uc03_search_vehicle_chassis
            ON auditcore.vehicle_records (tenant_id, upper(chassis_number) text_pattern_ops)
            WHERE chassis_number IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_uc03_search_vehicle_dms_reference
            ON auditcore.vehicle_records (tenant_id, upper(dms_reference) text_pattern_ops)
            WHERE dms_reference IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_uc03_search_vehicle_invoice_reference
            ON auditcore.vehicle_records (tenant_id, upper(invoice_reference) text_pattern_ops)
            WHERE invoice_reference IS NOT NULL;

        CREATE INDEX IF NOT EXISTS ix_uc03_search_registration_number
            ON auditcore.registration_records (
                tenant_id,
                upper(registration_number) text_pattern_ops
            )
            WHERE registration_number IS NOT NULL;

        CREATE INDEX IF NOT EXISTS ix_uc03_search_payment_reference
            ON auditcore.payments (tenant_id, upper(payment_reference) text_pattern_ops)
            WHERE payment_reference IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_search_payment_reference")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_search_registration_number")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_search_vehicle_invoice_reference")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_search_vehicle_dms_reference")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_search_vehicle_chassis")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_search_vehicle_vin")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_search_booking_reference")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_search_customer_mobile")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_search_customer_legal_name")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_search_customer_display_name")
