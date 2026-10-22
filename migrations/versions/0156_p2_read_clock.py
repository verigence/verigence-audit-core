"""0156_p2_read_clock — the 30-minute ceiling on reading an uploaded file.

Product owner, 2026-10-21: no file may take more than 30 minutes to be read
after it is uploaded; the worker fails any page (or file) still not settled
then and the PC is asked to resubmit it.

* p2_document_queue.read_clock_started_at_utc: when the page's 30 minutes
  restarted -- a person's Retry or Read again, or the night re-read of a
  page our setup refused. NULL means the clock runs from the upload (the
  batch's created_at_utc).
* p2_upload_batches.failure_reason: why a file failed as a whole (not
  uploaded, or not split into pages, within 30 minutes), for the PC's
  resubmit task and the failure report.
* p2_work_queue work type DI_DOCUMENT_WITHDRAW: a page the worker failed on
  the clock, or one uploaded again (superseded), has its Document
  Intelligence document deleted, so DI stops reading it and nothing is read
  twice (review of #469, 2026-10-21).

Additive: two nullable columns and one more allowed work type.
"""
from __future__ import annotations

from alembic import op

revision = "0156_p2_read_clock"
down_revision = "0155_p2_scheduled_runs"
branch_labels = None
depends_on = None


_WORK_TYPES = (
    "'SPLIT_BATCH','DOCUMENT_INGEST','DOCUMENT_RECONCILE','JOURNEY_RECONCILE',"
    "'STAGE_RECOMPUTE','CONTROL_EVALUATE','TASK_VERIFY','BATCH_GROUP','PAGE_REREAD'"
)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE auditcore.p2_document_queue ADD COLUMN IF NOT EXISTS read_clock_started_at_utc timestamptz;
        ALTER TABLE auditcore.p2_upload_batches ADD COLUMN IF NOT EXISTS failure_reason text;
        ALTER TABLE auditcore.p2_work_queue
          DROP CONSTRAINT IF EXISTS p2_work_queue_work_type_check,
          ADD CONSTRAINT p2_work_queue_work_type_check
            CHECK (work_type IN ({_WORK_TYPES},'DI_DOCUMENT_WITHDRAW'));
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM auditcore.p2_work_queue WHERE work_type='DI_DOCUMENT_WITHDRAW';
        ALTER TABLE auditcore.p2_work_queue
          DROP CONSTRAINT IF EXISTS p2_work_queue_work_type_check,
          ADD CONSTRAINT p2_work_queue_work_type_check CHECK (work_type IN ({_WORK_TYPES}));
        ALTER TABLE auditcore.p2_upload_batches DROP COLUMN IF EXISTS failure_reason;
        ALTER TABLE auditcore.p2_document_queue DROP COLUMN IF EXISTS read_clock_started_at_utc;
        """
    )
