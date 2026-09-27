"""0120_uc03_p2_document_replacement — durable replacement lineage.

A P2 replacement is a new upload batch that points at the document/evidence it
intends to replace. The old evidence remains permanent audit history and is only
marked SUPERSEDED after the replacement has reconciled successfully.
"""
from __future__ import annotations

from alembic import op

revision = "0120_uc03_p2_doc_replace"
down_revision = "0119_uc03_p2_doc_reconcile_rev"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_upload_batches
          ADD COLUMN replaces_document_id uuid,
          ADD COLUMN replaces_evidence_id uuid,
          ADD COLUMN replacement_applied_at_utc timestamptz;

        ALTER TABLE auditcore.p2_upload_batches
          ADD CONSTRAINT fk_p2_upload_replaces_evidence
          FOREIGN KEY (tenant_id, replaces_evidence_id)
          REFERENCES auditcore.evidence(tenant_id, evidence_id);

        CREATE INDEX ix_p2_upload_replacement
          ON auditcore.p2_upload_batches(
            tenant_id, journey_id, replaces_document_id
          )
          WHERE replaces_document_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS auditcore.ix_p2_upload_replacement;
        ALTER TABLE auditcore.p2_upload_batches
          DROP CONSTRAINT IF EXISTS fk_p2_upload_replaces_evidence,
          DROP COLUMN IF EXISTS replacement_applied_at_utc,
          DROP COLUMN IF EXISTS replaces_evidence_id,
          DROP COLUMN IF EXISTS replaces_document_id;
        """
    )
