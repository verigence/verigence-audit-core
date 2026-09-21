"""Add loan disbursement resolution columns to finance_records.

Revision ID: 0108_finance_disbursement
Revises: 0107_journey_housekeeping_corr
Create Date: 2026-09-21

finance_records.financed_amount is (correctly, confirmed by explicit
product decision) the RTO Challan's own hypothecation charges, not the
actual loan amount a bank/NBFC disbursed to the dealer -- no document
ever states that amount directly (RTO Challan and the insurance cover
note only ever confirm THAT a financer is involved, never how much).
The real amount has to be inferred from the journey's own payment
receipts: uc03_finance_disbursement_resolution.py tries an automatic
match first (eligible payment mode -- never Cash/UPI/Card/QR, matching
how Indian auto-loan disbursement actually works and Section 269ST's
cash-transaction cap -- plus financer-name and amount-plausibility
scoring), and falls back to a PC-confirmed Task Queue item, reusing the
exact same payment-picker UI on the standalone Journey Documents
correction path, when it can't decide on its own.

Additive only -- financed_amount itself is untouched.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0108_finance_disbursement"
down_revision = "0107_journey_housekeeping_corr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.finance_records
                ADD COLUMN IF NOT EXISTS loan_disbursement_amount numeric(18,2),
                ADD COLUMN IF NOT EXISTS loan_disbursement_payment_id uuid,
                ADD COLUMN IF NOT EXISTS loan_disbursement_confidence varchar(20),
                ADD COLUMN IF NOT EXISTS loan_disbursement_match_basis text
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.finance_records
                ADD CONSTRAINT ck_finance_records_disbursement_confidence
                CHECK (loan_disbursement_confidence IS NULL OR loan_disbursement_confidence IN (
                    'HIGH', 'MEDIUM', 'PC_CONFIRMED', 'AMBIGUOUS', 'UNVERIFIED'
                ))
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.finance_records
                ADD CONSTRAINT fk_finance_records_disbursement_payment
                FOREIGN KEY (tenant_id, loan_disbursement_payment_id)
                REFERENCES auditcore.payments (tenant_id, payment_id)
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.finance_records
                DROP CONSTRAINT IF EXISTS fk_finance_records_disbursement_payment,
                DROP CONSTRAINT IF EXISTS ck_finance_records_disbursement_confidence,
                DROP COLUMN IF EXISTS loan_disbursement_amount,
                DROP COLUMN IF EXISTS loan_disbursement_payment_id,
                DROP COLUMN IF EXISTS loan_disbursement_confidence,
                DROP COLUMN IF EXISTS loan_disbursement_match_basis
            """
        )
    )
