"""0139_p2_page_reread_work — the PAGE_REREAD work item.

A person (or the nightly sweep) asks Document Intelligence to read a page
again without re-uploading it (decision 2026-10-01: the "Read again" action
on the Upload / Edit Documents card and the journey Re-sync). The request is
queued work like every other Phase 2 step, so it survives restarts and is
never run twice in parallel.
"""
from __future__ import annotations

from alembic import op

revision = "0139_p2_page_reread_work"
down_revision = "0138_p2_management_referral"
branch_labels = None
depends_on = None

_WORK_TYPES = (
    "'SPLIT_BATCH','DOCUMENT_INGEST','DOCUMENT_RECONCILE','JOURNEY_RECONCILE',"
    "'STAGE_RECOMPUTE','CONTROL_EVALUATE','TASK_VERIFY','BATCH_GROUP'"
)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE auditcore.p2_work_queue
          DROP CONSTRAINT IF EXISTS p2_work_queue_work_type_check,
          ADD CONSTRAINT p2_work_queue_work_type_check
            CHECK (work_type IN ({_WORK_TYPES},'PAGE_REREAD'));
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM auditcore.p2_work_queue WHERE work_type='PAGE_REREAD';
        ALTER TABLE auditcore.p2_work_queue
          DROP CONSTRAINT IF EXISTS p2_work_queue_work_type_check,
          ADD CONSTRAINT p2_work_queue_work_type_check CHECK (work_type IN ({_WORK_TYPES}));
        """
    )
