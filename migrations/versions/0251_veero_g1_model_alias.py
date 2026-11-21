"""0251_veero_g1_model_alias — "VEERO G1" in the consumer bulletin is the Veero.

The Monthly Consumer Scheme Bulletin Nov'26 (Bhubaneswar RO, w.e.f. 4 Nov)
adds a row "VEERO G1 | V2 CNG / V4 CNG". No model or alias matched "VEERO G1",
so the ingest refused the whole bulletin ("a scheme row names a model that is
not known"; live dry run 2026-11-21). G1 is a generation marker, not another
model: the price list prices it as VEERO. Its CNG variants are not in the price
list and are reported as unresolved variants, as any variant not priced is.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0251_veero_g1_model_alias"
down_revision = "0250_dms_delivery_booking_date"
branch_labels = None
depends_on = None

_ALIASES = {"VEERO G1": "VEERO"}


def upgrade() -> None:
    op.get_bind().execute(
        text(
            """
            INSERT INTO auditcore.oem_model_aliases (oem_code, alias_text, canonical_model_name)
            VALUES ('MAHINDRA', :alias_text, :canonical_model_name)
            ON CONFLICT (oem_code, alias_text) DO UPDATE
              SET canonical_model_name = EXCLUDED.canonical_model_name
            """
        ),
        [{"alias_text": a, "canonical_model_name": c} for a, c in _ALIASES.items()],
    )


def downgrade() -> None:
    op.get_bind().execute(
        text("DELETE FROM auditcore.oem_model_aliases WHERE oem_code='MAHINDRA' AND alias_text = ANY(:a)"),
        {"a": list(_ALIASES)},
    )
