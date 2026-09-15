"""UC03 canonical payment mode type.

Revision ID: 0096_uc03_payment_mode_types
Revises: 0095_nightly_reprocessing_runs
Create Date: 2026-09-15

Payment receipts (dealer_receipt/payment_receipt) and bank statement lines
already carry a free-text payment_method_code/transaction_description --
audit evidence, exactly as extracted, left untouched here. This adds a
canonical, closed classification alongside it: IMPS, RTGS, NEFT, Bank
Transfer, Banker's Order, Pay Order, Cash, Cheque, Trade-In, Refund, and a
mandatory Others fallback so every row always resolves to exactly one type,
never left unclassified. auditcore.payments.payment_mode_code and
auditcore.bank_statement_lines.payment_mode_code both default to 'OTHERS'
and are backfilled below using the same classifier the application uses
going forward (audit_core.uc03_payment_mode.classify_payment_mode), imported
directly rather than re-implemented as SQL, so the one-time backfill can
never drift from live behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0096_uc03_payment_mode_types"
down_revision = "0095_nightly_reprocessing_runs"
branch_labels = None
depends_on = None

# migrations/ sits beside src/ in this repo; import the real classifier
# rather than re-implementing its rules as SQL for this one-time backfill.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def upgrade() -> None:
    from audit_core.uc03_payment_mode import PAYMENT_MODE_TYPES, classify_payment_mode

    conn = op.get_bind()

    conn.execute(
        text(
            """
            CREATE TABLE auditcore.payment_mode_types (
                code            varchar(40) PRIMARY KEY,
                label           varchar(80) NOT NULL,
                sort_order      integer NOT NULL,
                created_at_utc  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    )
    for sort_order, (code, label) in enumerate(PAYMENT_MODE_TYPES, start=1):
        conn.execute(
            text(
                """
                INSERT INTO auditcore.payment_mode_types (code, label, sort_order)
                VALUES (:code, :label, :sort_order)
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {"code": code, "label": label, "sort_order": sort_order},
        )

    conn.execute(
        text(
            """
            ALTER TABLE auditcore.payments
                ADD COLUMN payment_mode_code varchar(40) NOT NULL DEFAULT 'OTHERS'
                    REFERENCES auditcore.payment_mode_types(code)
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.bank_statement_lines
                ADD COLUMN payment_mode_code varchar(40) NOT NULL DEFAULT 'OTHERS'
                    REFERENCES auditcore.payment_mode_types(code)
            """
        )
    )

    payment_rows = conn.execute(
        text("SELECT tenant_id, payment_id, payment_method_code FROM auditcore.payments")
    ).all()
    for tenant_id, payment_id, payment_method_code in payment_rows:
        code = classify_payment_mode(payment_method_code)
        if code == "OTHERS":
            continue  # already the column default
        conn.execute(
            text(
                """
                UPDATE auditcore.payments
                SET payment_mode_code = :code
                WHERE tenant_id = :tenant_id AND payment_id = :payment_id
                """
            ),
            {"code": code, "tenant_id": tenant_id, "payment_id": payment_id},
        )

    bank_line_rows = conn.execute(
        text(
            """
            SELECT tenant_id, bank_statement_line_id, transaction_description, reference_no
            FROM auditcore.bank_statement_lines
            """
        )
    ).all()
    for tenant_id, bank_statement_line_id, transaction_description, reference_no in bank_line_rows:
        code = classify_payment_mode(transaction_description, reference_no)
        if code == "OTHERS":
            continue
        conn.execute(
            text(
                """
                UPDATE auditcore.bank_statement_lines
                SET payment_mode_code = :code
                WHERE tenant_id = :tenant_id AND bank_statement_line_id = :line_id
                """
            ),
            {"code": code, "tenant_id": tenant_id, "line_id": bank_statement_line_id},
        )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.bank_statement_lines DROP COLUMN IF EXISTS payment_mode_code")
    op.execute("ALTER TABLE auditcore.payments DROP COLUMN IF EXISTS payment_mode_code")
    op.execute("DROP TABLE IF EXISTS auditcore.payment_mode_types")
