"""0149_pricing_price_list_version — a Team Lead's approved price list version.

Found 2026-10-20 (full SKU run): two versions of the same price list can take
effect on the same day (OEM native consolidated v3 and v4, both from
2026-09-19), and pricing by date always takes the higher version. When the
model is only in the other one, approving "price on that list" by moving the
pricing date could never reach it. The approval now names the version itself:
bookings.pricing_price_list_version_id, read first by the price plan.
"""
from __future__ import annotations

from alembic import op

revision = "0149_pricing_price_list_version"
down_revision = "0148_vehicle_category"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.bookings
          ADD COLUMN IF NOT EXISTS pricing_price_list_version_id uuid
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.bookings DROP COLUMN IF EXISTS pricing_price_list_version_id")
