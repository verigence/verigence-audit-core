"""UC03 DOCUMENT_MISSING finding type.

Revision ID: 0069_uc03_document_missing
Revises: 0068_uc03_di_discovery_any_stage
Create Date: 2026-09-08

DI can terminate a document's processing as ``NOT_CONFIRMED`` (a non-retryable
extraction failure -- corrupt file, unreadable scan, wrong document entirely).
Today that state was only ever surfaced reactively: the PC would discover it
by trying to submit and hitting a 409 ("one or more documents failed
processing"), or by happening to notice the document's status in the capture
list. Nothing told them proactively.

This raises one ``DOCUMENT_MISSING`` finding per failed document, the moment
DI reports it -- async, from the same document-confirmation trigger that
already runs for every other outcome. It resolves automatically the moment a
reupload (a new document against the same requirement) confirms cleanly.

  finding_class = DATA_GAP  (owner PC, self-serve -- reupload the document)

Producer: ``audit_core.uc03_async_sync_tasks.sync_document_confirmation_status``
-- idempotent, self-heals, never raises.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0069_uc03_document_missing"
down_revision = "0068_uc03_di_discovery_any_stage"
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
                'DOCUMENT_MISSING', 'DATA_GAP', 'PC', 'SELF_SERVICE', 'ACTIVE',
                'Document Intelligence could not process an uploaded document (a '
                'non-retryable extraction failure -- unreadable scan, corrupt file, '
                'or the wrong document entirely). The PC needs to re-upload it.'
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
            "WHERE finding_type_code = 'DOCUMENT_MISSING'"
        )
    )
