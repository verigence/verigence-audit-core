"""0122_uc03_p2_supporting_pages — pages DI cannot classify are supporting evidence.

Real booking packets carry docket cover sheets, employer letters, PAN
verification printouts, e-mails and fee receipts alongside checklist
documents. They are kept as SUPPORTING evidence (visible, never blocking,
re-typeable by the PC) instead of raising a review task for every page.
"""
from __future__ import annotations

from alembic import op

revision = "0122_uc03_p2_supporting_pages"
down_revision = "0121_uc03_p2_runtime_hardening"
branch_labels = None
depends_on = None

_STATUSES = (
    "'QUEUED','PREPARING_PAGE','DI_UPLOAD_PREPARING','DI_UPLOADING','DI_FINALIZING',"
    "'CLASSIFYING','EXTRACTING','SYNCING_TO_AUDIT_CORE','READY','NEEDS_REVIEW',"
    "'RETRY_WAIT','FAILED','DEAD_LETTER','CANCELLED'"
)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE auditcore.p2_document_queue
          DROP CONSTRAINT IF EXISTS p2_document_queue_queue_status_check,
          ADD CONSTRAINT p2_document_queue_queue_status_check
            CHECK (queue_status IN ({_STATUSES},'SUPPORTING')),
          ADD COLUMN template_key varchar(120),
          ADD COLUMN type_overridden_by_actor_id varchar(160);
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE auditcore.p2_document_queue SET queue_status='NEEDS_REVIEW'
        WHERE queue_status='SUPPORTING';
        ALTER TABLE auditcore.p2_document_queue
          DROP COLUMN IF EXISTS type_overridden_by_actor_id,
          DROP COLUMN IF EXISTS template_key,
          DROP CONSTRAINT IF EXISTS p2_document_queue_queue_status_check,
          ADD CONSTRAINT p2_document_queue_queue_status_check
            CHECK (queue_status IN ({_STATUSES}));
        """
    )
