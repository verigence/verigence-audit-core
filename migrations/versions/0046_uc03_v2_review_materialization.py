"""Add only the V2 post-review derived/linkage fields that do not already exist.

Revision ID: 0046_uc03_v2_review_mat
Revises: 0045_journey_housekeeping
Create Date: 2026-08-30

The V2 review flow already owns Customer/Journey/Booking/Payment relationships. This
migration deliberately adds no duplicate business owner. It adds:
- the derived confirmed Booking date,
- an SKU-resolution remark beside the existing journey_products.product_sku_id,
- a stable DI document reference on Payment so V2 receipt materialization is
  idempotent without manufacturing legacy Evidence rows.
"""
from alembic import op

revision = "0046_uc03_v2_review_mat"
down_revision = "0045_journey_housekeeping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.bookings
            ADD COLUMN booking_confirmation_date date;

        ALTER TABLE auditcore.journey_products
            ADD COLUMN sku_resolution_remarks varchar(500);

        ALTER TABLE auditcore.payments
            ADD COLUMN source_di_document_id uuid;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_payments_v2_source_document
        ON auditcore.payments(tenant_id, journey_id, source_di_document_id)
        WHERE source_di_document_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.uq_payments_v2_source_document")
    op.execute(
        """
        ALTER TABLE auditcore.payments
            DROP COLUMN IF EXISTS source_di_document_id;

        ALTER TABLE auditcore.journey_products
            DROP COLUMN IF EXISTS sku_resolution_remarks;

        ALTER TABLE auditcore.bookings
            DROP COLUMN IF EXISTS booking_confirmation_date;
        """
    )
