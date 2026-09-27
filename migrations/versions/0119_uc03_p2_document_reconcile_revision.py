"""0119_uc03_p2_document_reconcile_revision — coalesce Journey reconciliation."""
from __future__ import annotations

from alembic import op

revision = "0119_uc03_p2_doc_reconcile_rev"
down_revision = "0118_uc03_p2_upload_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_journey_runtime
          ADD COLUMN document_revision bigint NOT NULL DEFAULT 0
            CHECK (document_revision >= 0);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_journey_runtime
          DROP COLUMN IF EXISTS document_revision;
        """
    )
