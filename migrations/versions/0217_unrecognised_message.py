"""0217_unrecognised_message -- the message on a page no document type fits (product owner 2026-11-12).

"If the document fails classification due to schema not available, then we have to send a
message that system doesn't understand the document. Pls recheck and if required submit
again. For issues with quality, continue with same error message." And: such pages "can be
uploaded in the photos ... otherwise they are copied to the extras folder". New pages carry the
new wording and are added to the journey's Extras (uc03_p2_worker.UNRECOGNISED_REASON,
uc03_p2_vehicle_photos.add_unrecognised_page). This does the same for the pages already
showing the old text: new text, and each kept in its journey's Extras (the stored page; no
copy). Open tasks take the new text on their next evaluation. Downgrade restores the text and
removes those Extras rows.
"""
from __future__ import annotations

from alembic import op

revision = "0217_unrecognised_message"
down_revision = "0216_drive_access"
branch_labels = None
depends_on = None

_OLD = "Kept as supporting evidence. Set the document type if this is a checklist document."
_NEW = (
    "The system did not understand this page. It is kept in Photos and other documents (Extras) "
    "and is not read. If it needs to be read, check with your TL and upload it again."
)


def upgrade() -> None:
    op.execute(
        f"UPDATE auditcore.p2_document_queue SET status_reason='{_NEW}', updated_at_utc=now() "
        f"WHERE status_reason='{_OLD}'"
    )
    op.execute(
        f"""
        INSERT INTO auditcore.delivery_vehicle_photos (tenant_id, journey_id, object_key, original_filename,
            content_type, size_bytes, uploaded_by_actor_id, client_upload_id, view_code, capture_source)
        SELECT q.tenant_id, q.journey_id, q.page_object_key,
               left(b.original_filename || ' - page ' || array_to_string(
                    COALESCE(q.page_numbers, ARRAY[q.page_number]), '-'), 255),
               CASE WHEN q.page_object_key LIKE '%.pdf' THEN 'application/pdf'
                    ELSE COALESCE(b.content_type, 'application/octet-stream') END,
               0, b.uploaded_by_actor_id, 'extra:' || q.queue_id::text, 'EXTRA', 'P2_UNRECOGNISED'
        FROM auditcore.p2_document_queue q
        JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
        WHERE q.status_reason='{_NEW}' AND q.queue_status='SUPPORTING' AND q.page_object_key IS NOT NULL
          AND q.journey_id IS NOT NULL
        ON CONFLICT (tenant_id, journey_id, client_upload_id) WHERE client_upload_id IS NOT NULL DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM auditcore.delivery_vehicle_photos WHERE capture_source='P2_UNRECOGNISED'")
    op.execute(
        f"UPDATE auditcore.p2_document_queue SET status_reason='{_OLD}', updated_at_utc=now() "
        f"WHERE status_reason='{_NEW}'"
    )
