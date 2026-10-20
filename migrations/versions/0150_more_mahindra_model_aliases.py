"""0150_more_mahindra_model_aliases — model names as dealers print them.

Found 2026-10-20 in the DEV SKU run: Booking Forms and invoices print the
pick-ups as "BOL MAXX PUP HD 1.7 LX" or "BOLERO MAXX PUP HD 2.0L VXI", and
the Bolero Neo as "Bol Neo N10 R". The longest alias prefix of the first
was "BOLERO" (-> the Bolero Neo), of the last nothing. These name the price
list's own models. Resolution takes the longest alias prefix, so the longer
spellings win over "BOLERO".
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0150_more_mahindra_model_aliases"
down_revision = "0149_pricing_price_list_version"
branch_labels = None
depends_on = None

_ALIASES = {
    "BOLERO MAXX PUP HD": "MAXX HD",
    "BOLERO MAXX PIK-UP HD": "MAXX HD",
    "BOLERO MAXX PIKUP HD": "MAXX HD",
    "BOLERO MAXX HD": "MAXX HD",
    "BOL MAXX PUP HD": "MAXX HD",
    "BOL MAXX HD": "MAXX HD",
    "MAXX PUP HD": "MAXX HD",
    "BOLERO MAXX PUP CITY": "MAXX CITY",
    "BOLERO MAXX PIK-UP CITY": "MAXX CITY",
    "BOLERO MAXX CITY": "MAXX CITY",
    "BOL MAXX PUP CITY": "MAXX CITY",
    "BOL MAXX CITY": "MAXX CITY",
    "MAXX PUP CITY": "MAXX CITY",
    "BOL NEO": "THE BOSS- BOLERO+NEO",
    "BOL NEO+": "THE BOSS- BOLERO+NEO",
    "BOL NEO PLUS": "THE BOSS- BOLERO+NEO",
}


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
