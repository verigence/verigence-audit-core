"""UC03 FINANCE_HYPOTHECATION_MISSING finding type.

Revision ID: 0077_finance_hypothecation
Revises: 0076_insurance_agent_misp
Create Date: 2026-09-10

Business ask: when a deal is financed (a loan/hypothecation), the pricing
should carry hypothecation (HP) charges -- if the journey's finance record
shows a financier but no HP charges amount, that's a gap worth a PC's
attention (upload/check the RTO Challan, which is where HP charges are
actually stated).

  finding_class = DATA_GAP  (owner PC, self-serve -- the PC can resolve this
  by ensuring the RTO Challan showing HP charges is uploaded/reviewed)

Producer: audit_core.uc03_delivery_review_materialization.
sync_finance_hypothecation_findings -- idempotent, self-heals, never raises.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0077_finance_hypothecation"
down_revision = "0076_insurance_agent_misp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        text(
            """
            INSERT INTO auditcore.finding_types
                (finding_type_code, finding_class, default_owner_role, resolution_mode,
                 status, description)
            VALUES (
                'FINANCE_HYPOTHECATION_MISSING', 'DATA_GAP', 'PC', 'SELF_SERVICE', 'ACTIVE',
                'This deal is financed (a financier/bank is on record) but no '
                'hypothecation (HP) charges amount has been captured. HP charges are '
                'stated on the RTO Challan -- the PC needs to upload it or confirm '
                'the amount if it is genuinely zero.'
            )
            ON CONFLICT (finding_type_code) DO UPDATE SET
                finding_class = 'DATA_GAP',
                default_owner_role = 'PC',
                resolution_mode = 'SELF_SERVICE',
                status = 'ACTIVE',
                description = EXCLUDED.description,
                updated_at_utc = now()
            """
        )
    )


def downgrade() -> None:
    op.get_bind().execute(
        text(
            "DELETE FROM auditcore.finding_types "
            "WHERE finding_type_code = 'FINANCE_HYPOTHECATION_MISSING'"
        )
    )
