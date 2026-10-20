"""0151_bolero_split_model_aliases — the Bolero, Bolero Neo and Bolero Neo Plus
are three models (2026-10-20 product owner rules).

September's price list printed all three as one model, "THE BOSS-
BOLERO+NEO"; October's prints "Bolero", "Bolero neo" and "Bolero Neo Plus",
and October's schemes and corporate policy give them different benefits.
The price-list ingest now files both months' rows under the three models
(by the variant's series letter: B4/B6/B8, N4..N11, P4/P10), named BOLERO,
BOLERO NEO and BOLERO NEO PLUS. These aliases, which pointed every Bolero
spelling at the combined September model, now point at the three.

Data: the September books already loaded keep their SKUs under the old
combined model; re-load the September price lists (then October) so the
Bolero rows of every month sit on the three models.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0151_bolero_split_model_aliases"
down_revision = "0150_more_mahindra_model_aliases"
branch_labels = None
depends_on = None

_ALIASES = {
    "BOLERO": "BOLERO",
    "BOLERO NEO": "BOLERO NEO",
    "BOLERO NEO+": "BOLERO NEO PLUS",
    "BOLERO NEO PLUS": "BOLERO NEO PLUS",
    "BOL NEO": "BOLERO NEO",
    "BOL NEO+": "BOLERO NEO PLUS",
    "BOL NEO PLUS": "BOLERO NEO PLUS",
}
_PREVIOUS = "THE BOSS- BOLERO+NEO"


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
        text(
            "UPDATE auditcore.oem_model_aliases SET canonical_model_name = :previous "
            "WHERE oem_code = 'MAHINDRA' AND alias_text = ANY(:a)"
        ),
        {"previous": _PREVIOUS, "a": list(_ALIASES)},
    )
