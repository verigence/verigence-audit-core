"""0258_bulk_file_by_upload_index -- find the bulk / Drive file of one upload by index.

Product owner, 23 Nov 2026: the Project Tracker's channel table is to show new Journeys
and updates to existing Journeys per channel. Each Journey-day takes the channel of its
first file: the bulk or Drive file read on that upload, looked up by the upload's batch.
p2_bulk_files had indexes by bulk upload, Journey and fingerprint, none by batch.
"""
from __future__ import annotations

from alembic import op

revision = "0258_bulk_file_by_upload_index"
down_revision = "0257_enquiry_is_not_booking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_p2_bulk_files_batch
          ON auditcore.p2_bulk_files (tenant_id, batch_id)
          WHERE batch_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_bulk_files_batch")
