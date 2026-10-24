"""0160_discount_variance -- every Journey's discount variance, from the invoice
(product owner, 2026-10-24).

``p2_journey_runtime.discount_variance`` is a jsonb list, one element per discount
whose invoice figure differs from the entitlement by more than Rs 100 (the same
tolerance as the price). It is written beside ``price_variance`` by the same refresh,
from the same Deal build, and is current exactly when ``price_variance`` is
(``price_variance_fact_version``).

Existing rows are marked stale (the version is cleared) so the sweep fills the
column for every Journey.

Additive and idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0160_discount_variance"
down_revision = "0159_price_variance_fact_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_journey_runtime ADD COLUMN IF NOT EXISTS discount_variance jsonb")
    op.execute("UPDATE auditcore.p2_journey_runtime SET price_variance_fact_version = NULL "
               "WHERE discount_variance IS NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_journey_runtime DROP COLUMN IF EXISTS discount_variance")
