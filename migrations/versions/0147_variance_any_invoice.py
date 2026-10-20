"""0147_variance_any_invoice — recompute the stored price variance of every
Journey with any customer invoice on file.

Decision 2026-10-20 (product owner), refining 0146: the moment ANY invoice
is uploaded the deal is priced from invoices only, and a mandatory
price-master line no invoice bills is a variance at nil. 0146 cleared only
journeys with a vehicle invoice; this clears every invoiced one so the
worker's backfill recomputes them with the new rule.
"""
from __future__ import annotations

from alembic import op

revision = "0147_variance_any_invoice"
down_revision = "0146_variance_from_invoice"
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
              AND s.source_document_type IN (
                'customer_invoice_dms', 'tax_invoice_tally', 'accessory_invoice_dms', 'accessory_invoice_tally',
                'ew_invoice', 'rsa_invoice', 'invoice_generic', 'credit_note', 'debit_note')
          )
        """
    )


def downgrade() -> None:
    pass
