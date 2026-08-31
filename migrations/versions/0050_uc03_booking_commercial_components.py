"""Add detailed Booking Form commercial components to existing Core storage.

Revision ID: 0050_uc03_commercial_components
Revises: 0049_uc03_delivery_applicability
Create Date: 2026-08-31

No new table is introduced. The existing booking_form_review_values row remains the
reviewed Booking Form owner, while commercial_lines continues to hold the per-line
commercial projection.
"""
from alembic import op

revision = "0050_uc03_commercial_components"
down_revision = "0049_uc03_delivery_applicability"
branch_labels = None
depends_on = None

_COLUMNS = (
    "sales_discount_amount",
    "buffer_discount_amount",
    "exchange_discount_amount",
    "corporate_discount_amount",
    "loyalty_discount_amount",
    "inhouse_insurance_discount_amount",
    "mr_discount_amount",
    "oem_referral_discount_amount",
    "other_discount_amount",
    "free_accessory_discount_amount",
    "essential_kit_amount",
    "genuine_accessories_amount",
    "non_genuine_accessories_amount",
    "fastag_amount",
    "extended_warranty_amount",
    "green_tax_amount",
    "service_package_amount",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(
            f"ALTER TABLE auditcore.booking_form_review_values "
            f"ADD COLUMN {column} numeric(18,2)"
        )


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.execute(
            f"ALTER TABLE auditcore.booking_form_review_values "
            f"DROP COLUMN IF EXISTS {column}"
        )
