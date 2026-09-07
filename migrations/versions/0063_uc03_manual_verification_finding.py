"""UC03 MANUAL_VERIFICATION finding type.

Revision ID: 0063
Revises: 0062
Create Date: 2026-09-07

A booking / delivery document whose machine-read fields include values below the
90% confidence bar becomes one ``MANUAL_VERIFICATION`` finding (one per stage x
document) in the PC's Review Queue, carrying the field list. The PC confirms or
corrects each value against the boxed document evidence and the finding resolves
— no need to reopen the journey and reach the review screen.

  finding_class = DATA_GAP  (owner PC, self-serve — the PC fixes it and it closes)

Reference seed only; the per-field decisions live on
``auditcore.journey_document_extracted_fields`` (``effective_value`` /
``reviewed_at_utc`` / ``is_modified``), the same store the review screen writes.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0063"
down_revision = "0062"
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
                'MANUAL_VERIFICATION', 'DATA_GAP', 'PC', 'SELF_SERVICE', 'ACTIVE',
                'One or more machine-read values on a booking/delivery document are '
                'below the 90% confidence threshold and need the PC to confirm or '
                'correct them.'
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
        text("DELETE FROM auditcore.finding_types WHERE finding_type_code = 'MANUAL_VERIFICATION'")
    )
