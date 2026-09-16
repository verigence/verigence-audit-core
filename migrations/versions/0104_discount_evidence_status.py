"""Evidence-verified applicability status for conditional discounts.

Revision ID: 0104_discount_evidence_status
Revises: 0103_sku_correction_task
Create Date: 2026-09-16

Corporate/Exchange/Scrappage discounts are *conditional* -- they only
genuinely apply when a matching proof document is on file (corporate ID,
vehicle RC, scrappage certificate). Cash discounts and value-in-kind
benefits (e.g. ACCESSORIES_KIT) have no such proof concept. Today
``eligibility_result`` only reflects master-data scoping (model/dealer/
customer-type match) and whether a value was claimed -- never whether the
claim is actually backed by evidence, so a PC-visible Deal-page "is this
discount genuinely applicable" indicator has nothing to read.

``evidence_status`` is additive, alongside (not replacing)
``eligibility_result``:
  VERIFIED      -- claimed, and the required proof document is on file
  MISSING       -- claimed, but the required proof document is absent
                   (this is also when the DISCOUNT_EVIDENCE_MISSING /
                   BK_DISCOUNT_EVIDENCE_MISSING finding is open)
  PENDING       -- eligible per masters, nothing claimed yet
  NOT_REQUIRED  -- not a conditional discount (cash, welcome, in-kind, ...)
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0104_discount_evidence_status"
down_revision = "0103_sku_correction_task"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.discount_applications
            ADD COLUMN evidence_status varchar(20)
                CHECK (evidence_status IN ('VERIFIED', 'MISSING', 'PENDING', 'NOT_REQUIRED'))
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("ALTER TABLE auditcore.discount_applications DROP COLUMN IF EXISTS evidence_status"))
