"""0146_variance_from_invoice — recompute the stored price variance of every
Journey whose vehicle invoice is on file.

Decision 2026-10-20 (product owner): once the vehicle invoice is uploaded it
is the deal's source of truth, and the variance is calculated from the
invoice only, never from the booking form. The stored
p2_journey_runtime.price_variance of an invoiced Journey was computed with
the booking fallback, so it is cleared here; the worker's backfill
(backfill_price_variance) recomputes every NULL with the new rule.
"""
from __future__ import annotations

from alembic import op

revision = "0146_variance_from_invoice"
down_revision = "0145_payments_without_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE auditcore.p2_journey_runtime r
        SET price_variance = NULL
        WHERE price_variance IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM auditcore.commercial_line_source_values s
            WHERE s.tenant_id = r.tenant_id AND s.journey_id = r.journey_id
              AND s.source_document_type IN ('customer_invoice_dms', 'tax_invoice_tally')
          )
        """
    )


def downgrade() -> None:
    pass
