"""UC03 WRONG_DOCUMENT finding type.

Revision ID: 0081_uc03_wrong_document
Revises: 0080_daily_ops_findings
Create Date: 2026-09-11

A Journey's documents must all belong to the same customer. Once a KYC
document (Aadhaar / PAN / a generic Customer KYC evidence) establishes the
customer's name, every other document carrying a person's name (Booking
Form, Insurance Cover, a Bank Approval Letter, any tax/customer invoice) is
checked against it. A mismatch means the wrong customer's paperwork was
attached to this journey -- a serious, adjudicated compliance issue, not a
self-serve data gap.

  finding_class = VIOLATION  (owner TL, adjudicated -- Accept confirms the
  document really belongs to someone else and must be replaced; Reject
  records the mismatch as a false positive, e.g. a legitimate name variant
  this check's fuzzy match under-scored)

Severity is always HIGH: a name mismatch across a journey's own documents is
never a minor issue.

Producer: audit_core.uc03_customer_identity_consistency.
sync_customer_identity_consistency -- idempotent, self-heals, never raises.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0081_uc03_wrong_document"
down_revision = "0080_daily_ops_findings"
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
                'WRONG_DOCUMENT', 'VIOLATION', 'TL', 'ADJUDICATED', 'ACTIVE',
                'A document''s extracted name does not match the customer''s KYC '
                'name on this journey. Confirm whether the wrong customer''s '
                'document was attached, or reject if this is a legitimate name '
                'variant.'
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
            "DELETE FROM auditcore.finding_types WHERE finding_type_code = 'WRONG_DOCUMENT'"
        )
    )
