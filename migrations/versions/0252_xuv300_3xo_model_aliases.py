"""0252_xuv300_3xo_model_aliases — "XUV 300" and "3XO" are the XUV 3XO.

Product owner 2026-11-21: map XUV 300 to the 3XO. The XUV 3XO is the XUV 300's
successor nameplate, and dealers still print the old name on booking forms,
NDCs and cost sheets ("XUV 300 / W6 (AXS) Petrol MT", "XUV300 AX7L PAT"), or
just "3XO". "XUV 700" -> XUV 7XO was already an alias; "XUV 300" was not, so
those forms named no model. On SHI-YK-261113-009 (Nirupama Rout) the booking form's
"XUV 300" matched nothing and a ledger's misread "XUV 7XO" set the car to the
XUV 7XO AX5 7 STR (Rs 17.88L against a Rs 9.99L booking). Live 2026-11-21: 14
booking forms say "XUV 300" / "XUV300", 35 say "3XO".

The EV spellings name the EV: the longest alias the words spell wins, so
"3XO EV" / "XUV 300 EV" never fall into the petrol 3XO.

Customer documents only (new column ``applies_to``: ALL | DOCUMENTS). The
OEM's own bulletins keep "XUV300" as its own line -- September's and
October's exchange bulletins print "XUV300 Rs 20,000" beside "XUV 3XO
(Including REVX) Rs 50,000", the old model's offer -- and an alias must never
merge two models' offers (2026-10-20). The bulletin and price-list ingests
read only ALL aliases; the SKU resolver reading booking forms, NDCs and cost
sheets reads every alias.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0252_xuv300_3xo_model_aliases"
down_revision = "0251_veero_g1_model_alias"
branch_labels = None
depends_on = None

_ALIASES = {
    "XUV 300": "XUV3XO",
    "XUV300": "XUV3XO",
    "XUV-300": "XUV3XO",
    "3XO": "XUV3XO",
    "XUV 300 EV": "XUV3XO EV",
    "XUV300 EV": "XUV3XO EV",
    "3XO EV": "XUV3XO EV",
}


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.oem_model_aliases "
        "ADD COLUMN applies_to varchar(16) NOT NULL DEFAULT 'ALL' "
        "CONSTRAINT oem_model_aliases_applies_to_chk CHECK (applies_to IN ('ALL', 'DOCUMENTS'))"
    )
    op.get_bind().execute(
        text(
            """
            INSERT INTO auditcore.oem_model_aliases (oem_code, alias_text, canonical_model_name, applies_to)
            VALUES ('MAHINDRA', :alias_text, :canonical_model_name, 'DOCUMENTS')
            ON CONFLICT (oem_code, alias_text) DO UPDATE
              SET canonical_model_name = EXCLUDED.canonical_model_name, applies_to = 'DOCUMENTS'
            """
        ),
        [{"alias_text": a, "canonical_model_name": c} for a, c in _ALIASES.items()],
    )


def downgrade() -> None:
    op.get_bind().execute(
        text("DELETE FROM auditcore.oem_model_aliases WHERE oem_code='MAHINDRA' AND alias_text = ANY(:a)"),
        {"a": list(_ALIASES)},
    )
    op.execute("ALTER TABLE auditcore.oem_model_aliases DROP COLUMN applies_to")
