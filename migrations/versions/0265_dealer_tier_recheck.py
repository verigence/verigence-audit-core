"""0265_dealer_tier_recheck -- every dealer discount given above its grid's
base tier is read again (2026-11-26).

Product owner, 2026-11-26 ("excess discount given Rs 2000, not in the excess discount
list"): UTK-SR-261030-001 gave "Other Discount" Rs 7,000 against a base tier of
Rs 5,000 with the customer's own insurance, and sat on the "depends on in-house
insurance" list. From this release a base-tier dealer discount whose insurance the
documents show was not in-house is excess (uc03_p2_journey360._settle_grid_tier), and
the grid tier reads the insurance decision the Deal reads
(uc03_deal_reconciliation._grid_tier). Read only on live before the change: 64 bookings
on that list, 42 of them with the documents showing the customer's own insurance.

Nothing about the bookings' facts moved, only the reading, so (as 0199 did):

* the cached Excess Discount rows of every booking with a dealer discount above its
  grid's base tier are marked stale: the worker's price-variance sweep rebuilds them,
  writing only the cache;
* each such booking still under review gets one stage recompute, so its deal is
  reconciled again (the tier) and EXCESS_DISCOUNT runs again.

A booking with no dealer discount above the base tier cannot change and is left alone.
"""
from __future__ import annotations

from alembic import op

revision = "0265_dealer_tier_recheck"
down_revision = "0264_drive_file_owner"
branch_labels = None
depends_on = None

# A dealer discount given above the base tier of its grid line, by more than the
# Rs 100 line tolerance (uc03_discount_given.LINE_TOLERANCE).
_ABOVE_BASE = r"""
    SELECT DISTINCT da.tenant_id, da.journey_id
    FROM auditcore.discount_applications da
    CROSS JOIN LATERAL (
        SELECT COALESCE(da.details->'reconciliation'->'grid', da.details->'grid')->'tiers'->>'BASE' AS base
    ) g
    WHERE da.discount_key = 'ADDITIONAL_DISCOUNT'
      AND g.base ~ '^[0-9]+(\.[0-9]+)?$'
      AND da.actual_discount_amount > g.base::numeric + 100
"""


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE auditcore.p2_journey_runtime r SET price_variance_fact_version = NULL
        FROM ({_ABOVE_BASE}) a
        WHERE r.tenant_id = a.tenant_id AND r.journey_id = a.journey_id
          AND r.price_variance_fact_version IS NOT NULL
        """
    )
    op.execute(
        f"""
        SELECT auditcore.p2_request_stage_recompute(j.tenant_id, j.journey_id)
        FROM auditcore.journeys j
        JOIN ({_ABOVE_BASE}) a ON a.tenant_id = j.tenant_id AND a.journey_id = j.journey_id
        WHERE j.review_completed_at_utc IS NULL
        """
    )


def downgrade() -> None:
    # The cache and the checks are rebuilt from the facts; nothing to restore.
    pass
