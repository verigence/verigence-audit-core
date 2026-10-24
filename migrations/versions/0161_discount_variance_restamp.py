"""0161_discount_variance_restamp -- recompute the Journeys left without a discount variance.

Migration 0160 added ``p2_journey_runtime.discount_variance`` and cleared the
cache version so the sweep would fill it. In the minutes between that migration
and the new worker starting, the previous worker build kept sweeping: it
recomputed price variance, stamped the version as current, and knew nothing of
the new column. Those rows are "current" with no discount variance, so the
sweep no longer picks them up (2026-10-24: 50 of 233 Journeys in Mahindra Orissa).

Clearing the version again, for exactly the rows that still have no discount
variance, hands them back to the sweep. Additive and idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0161_discount_variance_restamp"
down_revision = "0160_discount_variance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE auditcore.p2_journey_runtime SET price_variance_fact_version = NULL "
               "WHERE discount_variance IS NULL")


def downgrade() -> None:
    pass
