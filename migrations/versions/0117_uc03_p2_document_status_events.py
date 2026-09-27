"""0117_uc03_p2_document_status_events — low-cost UI processing feed.

Every durable p2_document_queue state transition emits one small activity row.
The Web UI can poll the monotonically increasing event cursor while processing
is active and refresh document data only when something actually changed.
"""
from __future__ import annotations

from alembic import op

revision = "0117_uc03_p2_doc_status_events"
down_revision = "0116_uc03_p2_control_bridge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.p2_emit_document_status_event()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' OR NEW.queue_status IS DISTINCT FROM OLD.queue_status THEN
                INSERT INTO auditcore.p2_activity_events (
                    tenant_id, journey_id, event_type, subject_type,
                    subject_id, details, correlation_id
                ) VALUES (
                    NEW.tenant_id,
                    NEW.journey_id,
                    'DOCUMENT_STATUS_CHANGED',
                    'DOCUMENT_PAGE',
                    NEW.queue_id::text,
                    jsonb_build_object(
                        'queueId', NEW.queue_id::text,
                        'batchId', NEW.batch_id::text,
                        'pageNumber', NEW.page_number,
                        'status', NEW.queue_status,
                        'documentType', NEW.classified_document_type,
                        'businessStage', NEW.business_stage,
                        'extractedFieldCount', NEW.extracted_field_count
                    ),
                    NEW.correlation_id
                );
            END IF;
            RETURN NEW;
        END;
        $$;

        DROP TRIGGER IF EXISTS trg_p2_document_status_event
          ON auditcore.p2_document_queue;
        CREATE TRIGGER trg_p2_document_status_event
          AFTER INSERT OR UPDATE OF queue_status
          ON auditcore.p2_document_queue
          FOR EACH ROW EXECUTE FUNCTION auditcore.p2_emit_document_status_event();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_p2_document_status_event
          ON auditcore.p2_document_queue;
        DROP FUNCTION IF EXISTS auditcore.p2_emit_document_status_event();
        """
    )
