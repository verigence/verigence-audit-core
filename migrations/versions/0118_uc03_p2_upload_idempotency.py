"""0118_uc03_p2_upload_idempotency — make upload initialization retry-safe."""
from __future__ import annotations

from alembic import op

revision = "0118_uc03_p2_upload_idempotency"
down_revision = "0117_uc03_p2_doc_status_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_upload_batches
          ADD COLUMN client_upload_id varchar(160);

        CREATE UNIQUE INDEX ux_p2_upload_batches_client_upload
          ON auditcore.p2_upload_batches(
            tenant_id, journey_id, client_upload_id
          )
          WHERE client_upload_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS auditcore.ux_p2_upload_batches_client_upload;
        ALTER TABLE auditcore.p2_upload_batches
          DROP COLUMN IF EXISTS client_upload_id;
        """
    )
