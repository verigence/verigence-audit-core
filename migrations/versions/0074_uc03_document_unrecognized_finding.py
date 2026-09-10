"""UC03 DOCUMENT_UNRECOGNIZED finding type.

Revision ID: 0074_uc03_document_unrecognized
Revises: 0073_uc03_credit_note_gst_decl
Create Date: 2026-09-10

DI's Capture V2 classifier sets a document's state to UNKNOWN when its best
guess against the tenant's known document types still falls below the
acceptance threshold -- it genuinely doesn't know what the document is. That
state was previously invisible: DI never arms the audit-link webhook for it
(no evidence row, so Audit Core's document-sync pipeline never sees it), and
the capture screen's own card status only distinguishes PROCESSED/FAILED --
everything else, UNKNOWN included, renders as plain "Uploaded" with no sign
anything needs attention.

This raises one DOCUMENT_UNRECOGNIZED finding per such document, carrying its
filename and view link, the moment a capture-screen read reconciles the
journey's live DI document list.

  finding_class = DATA_GAP  (owner PC, self-serve -- open it, confirm what it
  actually is, re-upload as the correct type or ask an Admin to register a
  new document type)

Producer: audit_core.uc03_document_unrecognized.sync_document_unrecognized_findings
-- idempotent, self-heals, never raises.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0074_uc03_document_unrecognized"
down_revision = "0073_uc03_credit_note_gst_decl"
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
                'DOCUMENT_UNRECOGNIZED', 'DATA_GAP', 'PC', 'SELF_SERVICE', 'ACTIVE',
                'Document Intelligence could not confidently identify an uploaded '
                'document as any known type. The PC needs to open it, confirm what '
                'it actually is, and either re-upload it as the correct type or ask '
                'an Admin to register a new document type.'
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
            "WHERE finding_type_code = 'DOCUMENT_UNRECOGNIZED'"
        )
    )
