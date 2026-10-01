"""0141_p2_runtime_price_variance — one stored price variance per Journey.

The Journey list used its own SQL for "price variance" while the Deal tab
builds the number in Python, so the two disagreed (the list ignored invoice
taxes, discounts, FASTag and the insurance invoice). The worker now stores the
Deal's own current-vs-standard variance here whenever it recomputes a Journey,
and the list reads it. NULL means "not computed yet": the list then falls back
to its previous calculation, so nothing changes for a Journey until it is.
"""
from __future__ import annotations

from alembic import op

revision = "0141_p2_runtime_price_variance"
down_revision = "0140_p2_readable_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_journey_runtime ADD COLUMN IF NOT EXISTS price_variance numeric(18,2);")


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_journey_runtime DROP COLUMN IF EXISTS price_variance;")
