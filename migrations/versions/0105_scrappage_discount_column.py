"""Add the missing scrappage_discount_amount column to the reviewed Booking Form.

Revision ID: 0105_scrappage_discount_col
Revises: 0104_discount_evidence_status
Create Date: 2026-09-16

Confirmed bug: ``uc03_masters_alignment.DISCOUNT_ACTUAL_FIELD_TO_BENEFIT_KEY``
has always listed ``scrappage_discount_amount`` alongside
``corporate_discount_amount`` / ``exchange_discount_amount``, but unlike
those two (added by migration 0050), this column never actually existed on
``auditcore.booking_form_review_values`` -- so a scrappage amount the
Booking Form itself shows could never reach
``discount_applications.actual_discount_amount`` via
``uc03_deal_reconciliation._actual_discounts_by_benefit``, which reads only
this table. (``uc03_booking_confirmation_rules.py``'s own discount-evidence
check is unaffected -- it reads the DI-extracted field directly, not this
reviewed table.)
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0105_scrappage_discount_col"
down_revision = "0104_discount_evidence_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            "ALTER TABLE auditcore.booking_form_review_values "
            "ADD COLUMN scrappage_discount_amount numeric(18,2)"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            "ALTER TABLE auditcore.booking_form_review_values "
            "DROP COLUMN IF EXISTS scrappage_discount_amount"
        )
    )
