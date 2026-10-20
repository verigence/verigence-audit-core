"""0152_sku_match_basis — how a Journey's SKU was set, and on what evidence.

Product owner rules 2026-10-20 (R3): a SKU is identified by the words its
documents print; a price may only check it, never choose it. Automatic
matches are not "Confirmed" -- only a PC or Team Lead decision is -- and an
automatic match is replaced by later evidence or by the PC's direct pick.

journey_products.selection_status keeps its CHECK ('TENTATIVE' /
'CONFIRMED'): automatic pins are now TENTATIVE, PC / TL decisions CONFIRMED.
This migration adds:

  * match_basis -- AUTO_MATCHED (words name one SKU, booking price agrees),
    AUTO_PRICE_MISMATCH (words name one SKU, price differs or missing; a PC
    task is open), AUTO_INVOICE_MATCHED (a vehicle invoice's words and price
    agree), AUTO_LEGACY (pinned by the resolver before this rule; it may
    have been chosen by price and is re-checked by the "reresolve" SKU run),
    PC_CONFIRMED, TL_APPROVED;
  * match_evidence -- the document, its words and the prices compared.

Aliases: "SCORPIO-N" names the new Scorpio N (October's spelling of
September's "New SCORPIO N"), as the price masters' model identity has it
(oem_price_masters.price_list_model_key); 0061 pointed it at the old
SCORPIO N. The other renames (NEW THAR, SCORPIO, XUV 3XO, XEV 9S, XUV 3XO
EV, the three Bolero models) already agree with that identity.

Backfill: existing pins are classified from selection_method. The
resolver's own earlier pins (MODEL_RESOLUTION_SYNC_V1 / _INVOICE_FALLBACK_V1
/ _INVOICE_OVERRIDE_V1) become TENTATIVE + AUTO_LEGACY, so they can be
replaced without a Team Lead proposal; the PC's picks and the TL-approved
corrections keep CONFIRMED. Nothing is deleted; the downgrade restores
CONFIRMED on those rows.
"""
from __future__ import annotations

from alembic import op

revision = "0152_sku_match_basis"
down_revision = "0151_bolero_split_model_aliases"
branch_labels = None
depends_on = None

_AUTOMATIC_METHODS = (
    "'MODEL_RESOLUTION_SYNC_V1', 'MODEL_RESOLUTION_INVOICE_FALLBACK_V1', 'MODEL_RESOLUTION_INVOICE_OVERRIDE_V1'"
)


def upgrade() -> None:
    op.execute(
        """
        UPDATE auditcore.oem_model_aliases SET canonical_model_name = 'NEW SCORPIO N', updated_at_utc = now()
        WHERE oem_code = 'MAHINDRA' AND alias_text = 'SCORPIO-N'
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_products
          ADD COLUMN IF NOT EXISTS match_basis varchar(30),
          ADD COLUMN IF NOT EXISTS match_evidence jsonb,
          ADD CONSTRAINT ck_journey_products_match_basis
            CHECK (match_basis IS NULL OR match_basis IN (
              'AUTO_MATCHED', 'AUTO_PRICE_MISMATCH', 'AUTO_INVOICE_MATCHED', 'AUTO_LEGACY',
              'PC_CONFIRMED', 'TL_APPROVED'))
        """
    )
    op.execute(
        f"""
        UPDATE auditcore.journey_products
        SET match_basis = 'AUTO_LEGACY', selection_status = 'TENTATIVE'
        WHERE product_sku_id IS NOT NULL AND selection_method IN ({_AUTOMATIC_METHODS})
        """
    )
    op.execute(
        """
        UPDATE auditcore.journey_products SET match_basis = 'PC_CONFIRMED'
        WHERE product_sku_id IS NOT NULL AND selection_method = 'MODEL_RESOLUTION_PC_PICK_V1'
        """
    )
    op.execute(
        """
        UPDATE auditcore.journey_products SET match_basis = 'TL_APPROVED'
        WHERE product_sku_id IS NOT NULL AND selection_method = 'MODEL_SELECTION_CORRECTION_V1'
        """
    )


def downgrade() -> None:
    op.execute(
        "UPDATE auditcore.oem_model_aliases SET canonical_model_name = 'SCORPIO N' "
        "WHERE oem_code = 'MAHINDRA' AND alias_text = 'SCORPIO-N'"
    )
    op.execute(
        """
        UPDATE auditcore.journey_products SET selection_status = 'CONFIRMED'
        WHERE product_sku_id IS NOT NULL AND selection_status = 'TENTATIVE'
          AND match_basis LIKE 'AUTO%'
        """
    )
    op.execute("ALTER TABLE auditcore.journey_products DROP CONSTRAINT IF EXISTS ck_journey_products_match_basis")
    op.execute(
        "ALTER TABLE auditcore.journey_products DROP COLUMN IF EXISTS match_evidence, "
        "DROP COLUMN IF EXISTS match_basis"
    )
