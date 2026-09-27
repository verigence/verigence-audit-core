"""0123_uc03_p2_page_grouping — multi-document PDFs become documents, not pages.

A multi-page upload is split into PAGE units which DI classifies. Pages that
form one business document (Aadhaar front/back, a 3-page GST certificate, the
Booking Commitment Form + Order Taking Form pair) are then combined into a
GROUP unit: one merged PDF submitted to DI as a single document, so extraction
sees the whole document. The PAGE fragments become MERGED and are retired
(evidence voided, DI copy removed); the original upload is always retained.
"""
from __future__ import annotations

from alembic import op

revision = "0123_uc03_p2_page_grouping"
down_revision = "0122_uc03_p2_supporting_pages"
branch_labels = None
depends_on = None

_BASE_STATUSES = (
    "'QUEUED','PREPARING_PAGE','DI_UPLOAD_PREPARING','DI_UPLOADING','DI_FINALIZING',"
    "'CLASSIFYING','EXTRACTING','SYNCING_TO_AUDIT_CORE','READY','NEEDS_REVIEW',"
    "'RETRY_WAIT','FAILED','DEAD_LETTER','CANCELLED','SUPPORTING'"
)
_WORK_TYPES = (
    "'SPLIT_BATCH','DOCUMENT_INGEST','DOCUMENT_RECONCILE','JOURNEY_RECONCILE',"
    "'STAGE_RECOMPUTE','CONTROL_EVALUATE','TASK_VERIFY'"
)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE auditcore.p2_document_queue
          ADD COLUMN unit_kind varchar(10) NOT NULL DEFAULT 'PAGE'
            CHECK (unit_kind IN ('PAGE','GROUP')),
          ADD COLUMN page_numbers integer[],
          ADD COLUMN merged_into_queue_id uuid,
          ADD COLUMN retired_at_utc timestamptz,
          ADD COLUMN group_source varchar(20)
            CHECK (group_source IS NULL OR group_source IN ('SYSTEM','PC')),
          ADD COLUMN candidate_override jsonb,
          DROP CONSTRAINT IF EXISTS p2_document_queue_queue_status_check,
          ADD CONSTRAINT p2_document_queue_queue_status_check
            CHECK (queue_status IN ({_BASE_STATUSES},'MERGED')),
          DROP CONSTRAINT IF EXISTS p2_document_queue_tenant_id_batch_id_page_number_key;

        UPDATE auditcore.p2_document_queue SET page_numbers=ARRAY[page_number]
        WHERE page_numbers IS NULL;

        CREATE UNIQUE INDEX ux_p2_document_queue_page
          ON auditcore.p2_document_queue(tenant_id, batch_id, page_number)
          WHERE unit_kind='PAGE';
        CREATE UNIQUE INDEX ux_p2_document_queue_group
          ON auditcore.p2_document_queue(tenant_id, batch_id, page_numbers)
          WHERE unit_kind='GROUP' AND queue_status <> 'CANCELLED';
        CREATE INDEX ix_p2_document_queue_merged
          ON auditcore.p2_document_queue(tenant_id, merged_into_queue_id)
          WHERE merged_into_queue_id IS NOT NULL;

        ALTER TABLE auditcore.p2_upload_batches
          ADD COLUMN grouping_status varchar(20) NOT NULL DEFAULT 'NOT_NEEDED'
            CHECK (grouping_status IN ('NOT_NEEDED','PENDING','GROUPING','GROUPED','FAILED')),
          ADD COLUMN grouped_at_utc timestamptz;

        ALTER TABLE auditcore.p2_work_queue
          DROP CONSTRAINT IF EXISTS p2_work_queue_work_type_check,
          ADD CONSTRAINT p2_work_queue_work_type_check
            CHECK (work_type IN ({_WORK_TYPES},'BATCH_GROUP'));
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM auditcore.p2_work_queue WHERE work_type='BATCH_GROUP';
        ALTER TABLE auditcore.p2_work_queue
          DROP CONSTRAINT IF EXISTS p2_work_queue_work_type_check,
          ADD CONSTRAINT p2_work_queue_work_type_check CHECK (work_type IN ({_WORK_TYPES}));
        ALTER TABLE auditcore.p2_upload_batches
          DROP COLUMN IF EXISTS grouped_at_utc,
          DROP COLUMN IF EXISTS grouping_status;
        DROP INDEX IF EXISTS auditcore.ix_p2_document_queue_merged;
        DROP INDEX IF EXISTS auditcore.ux_p2_document_queue_group;
        DROP INDEX IF EXISTS auditcore.ux_p2_document_queue_page;
        DELETE FROM auditcore.p2_document_queue WHERE unit_kind='GROUP';
        UPDATE auditcore.p2_document_queue SET queue_status='CANCELLED' WHERE queue_status='MERGED';
        ALTER TABLE auditcore.p2_document_queue
          DROP CONSTRAINT IF EXISTS p2_document_queue_queue_status_check,
          ADD CONSTRAINT p2_document_queue_queue_status_check CHECK (queue_status IN ({_BASE_STATUSES})),
          ADD CONSTRAINT p2_document_queue_tenant_id_batch_id_page_number_key
            UNIQUE (tenant_id, batch_id, page_number),
          DROP COLUMN IF EXISTS candidate_override,
          DROP COLUMN IF EXISTS group_source,
          DROP COLUMN IF EXISTS retired_at_utc,
          DROP COLUMN IF EXISTS merged_into_queue_id,
          DROP COLUMN IF EXISTS page_numbers,
          DROP COLUMN IF EXISTS unit_kind;
        """
    )
