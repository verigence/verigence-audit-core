"""0212_source_values_withdrawn -- a figure a document no longer gives stops counting (2026-11-12).

``commercial_line_source_values`` keeps what each document reported for each line of the Deal.
A row was only ever written, never taken back: when a document was read again and no longer
gave a figure, the old row stayed and went on counting for as long as the document was on
file. Found on SHI-SS-261027-024 (Mahindra Orissa): the NDC was read again on 5 Nov and has
no insurance row, but its earlier "insurance Rs 82,000" row stood, so the insurance was
decided in-house ("the dealer's statement charges the premium") although the policy is the
customer's own (agent: DIRECT BUSINESS) and the booking docket says self.

The runtime role may not delete from this table (0101), and the history is worth keeping:
``withdrawn_at_utc`` marks a row the document no longer gives. Every reader goes through
``uc03_deal_source_history.ACTIVE_SOURCE_CONDITION``, which now leaves withdrawn rows out; a
row comes back the moment its document gives the figure again.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0212_source_values_withdrawn"
down_revision = "0211_journey_classifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.commercial_line_source_values ADD COLUMN IF NOT EXISTS withdrawn_at_utc timestamptz"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.commercial_line_source_values DROP COLUMN IF EXISTS withdrawn_at_utc")
