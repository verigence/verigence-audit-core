"""Per-document source values and printed GST rates on invoices.

Revision ID: 0153_source_values_per_document
Revises: 0150_more_mahindra_model_aliases
Create Date: 2026-10-20

2026-10-20 product owner rules (R1, R2): each invoice serves only its own
lines and is never clubbed with, or silently replaced by, another invoice.

``commercial_line_source_values`` kept one row per (journey, kind,
component, document *type*): a second invoice of the same type overwrote the
first, and the Deal sheet ended up mixing figures from two invoices (the
discount of one, the taxes of another). The key now includes the source
document, so every invoice keeps its own row. A source with no document id
(the booking form's discount rows) still has one row per type: the nil UUID
stands in for "no document" in the unique index.

Additive only: the primary key is dropped and replaced by a unique index with
the document in it; no row is changed or removed. Rows written before this
migration hold the last-written document of each type; re-materialising a
journey records every document again.

``invoice_review_values`` gains the printed CGST / SGST / IGST rates, which
the ex-showroom (selling price plus GST at that invoice's own rate) needs
when an invoice prints the rate.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0153_source_values_per_document"
down_revision = "0150_more_mahindra_model_aliases"
branch_labels = None
depends_on = None

_NO_DOCUMENT = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.commercial_line_source_values
            DROP CONSTRAINT IF EXISTS commercial_line_source_values_pkey
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_commercial_line_source_values_document
            ON auditcore.commercial_line_source_values (
                tenant_id, journey_id, line_kind, component_key, source_document_type,
                (COALESCE(source_document_id, '{_NO_DOCUMENT}'::uuid))
            )
            """
        )
    )
    for column in ("cgst_rate", "sgst_rate", "igst_rate"):
        conn.execute(
            text(f"ALTER TABLE auditcore.invoice_review_values ADD COLUMN IF NOT EXISTS {column} numeric(9,4)")
        )


def downgrade() -> None:
    conn = op.get_bind()
    for column in ("cgst_rate", "sgst_rate", "igst_rate"):
        conn.execute(text(f"ALTER TABLE auditcore.invoice_review_values DROP COLUMN IF EXISTS {column}"))
    conn.execute(text("DROP INDEX IF EXISTS auditcore.ux_commercial_line_source_values_document"))
    # The old key cannot hold two documents of one type; this fails (and the
    # downgrade stops) rather than delete any of them.
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.commercial_line_source_values
            ADD CONSTRAINT commercial_line_source_values_pkey
            PRIMARY KEY (tenant_id, journey_id, line_kind, component_key, source_document_type)
            """
        )
    )
