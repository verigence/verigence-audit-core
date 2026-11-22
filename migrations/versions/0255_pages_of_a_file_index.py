"""0255_pages_of_a_file_index -- find the pages of one uploaded file by index.

Product owner, 22 Nov 2026: "what happens with the failed journey uploads.. are
they being reloaded or they silently got missed out". The job that uploads again,
from storage, the files that failed on our side (OUR_SIDE_REUPLOAD, 2 Nov) failed
on its first query: "canceling statement due to statement timeout" (10 seconds).

p2_document_queue had no index by file. Its two indexes that start with the file
are partial (one for page units, one for document units not cancelled), so "the
units of this file with this failure" read the whole table -- 49,348 rows, 74 MB
on Mahindra Orissa -- once per file and per count: four times for each of 4,977
files. On 16 Nov the same query still finished; the table has grown past it.

One plain index by file serves every look-up of a file's pages and documents.
"""
from __future__ import annotations

from alembic import op

revision = "0255_pages_of_a_file_index"
down_revision = "0254_asked_again_while_running"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_p2_document_queue_batch
          ON auditcore.p2_document_queue (tenant_id, batch_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_document_queue_batch")
