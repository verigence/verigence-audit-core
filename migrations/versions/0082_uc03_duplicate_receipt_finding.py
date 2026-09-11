"""UC03 DUPLICATE_RECEIPT finding type.

Revision ID: 0082_uc03_duplicate_receipt
Revises: 0081_uc03_wrong_document
Create Date: 2026-09-11

The same physical receipt uploaded more than once (a PC re-uploading by
accident, or a duplicate DI classification) must not be counted twice when
summing what a customer paid -- 5 uploads of one real ₹2L receipt is ₹2L
paid, not ₹10L. This raises one DUPLICATE_RECEIPT finding per duplicate
group, scoped to receipts of the *same* document type only (dealer_receipt
compared against dealer_receipt, payment_receipt against payment_receipt --
never mixed, since a Booking advance receipt and a Delivery balance receipt
legitimately coexist for the same amount).

  finding_class = VIOLATION  (owner TL, adjudicated -- Accept confirms it's
  really a duplicate upload to be removed/voided; Reject records that these
  are two genuinely separate payments the automated match over-called)

Severity is always HIGH: an uncaught duplicate directly inflates the money
this Journey appears to have collected.

Producer: audit_core.uc03_duplicate_receipt_detection.
sync_duplicate_receipt_detection -- idempotent, self-heals, never raises.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0082_uc03_duplicate_receipt"
down_revision = "0081_uc03_wrong_document"
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
                'DUPLICATE_RECEIPT', 'VIOLATION', 'TL', 'ADJUDICATED', 'ACTIVE',
                'Two or more receipts of the same type (dealer_receipt or '
                'payment_receipt) on this Journey appear to be the same physical '
                'receipt uploaded more than once. Confirm whether it is a genuine '
                'duplicate upload to be voided, or two separate real payments.'
            )
            ON CONFLICT (finding_type_code) DO UPDATE SET
                finding_class = 'VIOLATION',
                default_owner_role = 'TL',
                resolution_mode = 'ADJUDICATED',
                status = 'ACTIVE',
                description = EXCLUDED.description,
                updated_at_utc = now()
            """
        )
    )


def downgrade() -> None:
    op.get_bind().execute(
        text(
            "DELETE FROM auditcore.finding_types WHERE finding_type_code = 'DUPLICATE_RECEIPT'"
        )
    )
