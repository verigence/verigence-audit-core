"""Fix the orphaned display_label for the booking_payment_receipt requirement.

Revision ID: 0094_fix_payment_receipt_label
Revises: 0093_housekeeping_scrappage
Create Date: 2026-09-14

Migration 0022 seeded every journey's actual Booking requirement row under
the key ``booking_payment_receipt`` (see ``document_requirement_items`` /
``initialize_uc03_booking_requirements()``). Migration 0036, adding the
*label* table (``document_capture_v2_requirement_policy``), named what was
clearly meant to be the same requirement ``minimum_booking_payment_proof``
instead -- a naming drift, not a distinct requirement (nothing has ever
created a journey_document_requirements row under that key). The two keys
never matched, so ``_base_requirements``'s ``COALESCE(p.display_label,
jdr.requirement_key)`` has fallen back to the raw key on every Booking
checklist ever rendered: seen live today as a raw "booking_payment_receipt"
row sitting next to "Booking Form".

Renaming in place (not delete+insert) preserves the row's sort_order (40,
already correctly positioned right after Booking Form) and its created_at.
"""
from alembic import op
from sqlalchemy import text

revision = "0094_fix_payment_receipt_label"
down_revision = "0093_housekeeping_scrappage"
branch_labels = None
depends_on = None

_OLD_KEY = "minimum_booking_payment_proof"
_NEW_KEY = "booking_payment_receipt"
_LABEL = "Booking Payment Receipt"
_OLD_LABEL = "Minimum Booking Amount Proof"


def upgrade() -> None:
    op.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_requirement_policy
            SET requirement_key=:new_key,
                display_label=:label,
                updated_at_utc=now()
            WHERE requirement_key=:old_key
            """
        ).bindparams(new_key=_NEW_KEY, label=_LABEL, old_key=_OLD_KEY)
    )


def downgrade() -> None:
    op.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_requirement_policy
            SET requirement_key=:old_key,
                display_label=:old_label,
                updated_at_utc=now()
            WHERE requirement_key=:new_key
            """
        ).bindparams(old_key=_OLD_KEY, old_label=_OLD_LABEL, new_key=_NEW_KEY)
    )
