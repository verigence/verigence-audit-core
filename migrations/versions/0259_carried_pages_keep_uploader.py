"""0259_carried_pages_keep_uploader -- pages carried to an earlier booking are the uploader's (2026-11-24).

Product owner, 24 Nov 2026: Bodhisundar Mohanty (Aditya Motors - Cuttack) put 15 files in Drive on
23 Nov; his day showed 13 and the other two were "not visible anywhere in failures". Neither had
failed: each was read on a Journey of its own and its pages carried to the customer's earlier
booking (uc03_p2_same_file.carry_documents), and the carried uploads were recorded as uploaded by
the earlier booking's creator. Swagatika Rout's delivery file, carried to a booking Radhakanta Das
made in October, counted as Radhakanta's delivery.

From now on a carried page keeps the person who put it in. This re-files the uploads carried
before: the person who put the file in is the Drive / bulk upload's PC when the copy was a
bulk-read Journey, else the copy Journey's PC (its deletion record, or the Journey itself if it
was kept). A carried upload is found by its copy activity (DOCUMENT_COPIED_FROM_DUPLICATE, job
"same-file:<the copy Journey>"), so nothing else is touched.
"""
from __future__ import annotations

from alembic import op

revision = "0259_carried_pages_keep_uploader"
down_revision = "0258_bulk_file_by_upload_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        WITH carried AS (
            SELECT ub.tenant_id, ub.batch_id, ub.uploaded_by_actor_id AS was,
                   CAST(substr(a.details->>'job', 11) AS uuid) AS copy_journey_id
            FROM auditcore.p2_upload_batches ub
            JOIN auditcore.p2_activity_events a
              ON a.tenant_id = ub.tenant_id AND a.journey_id = ub.journey_id
             AND a.subject_id = CAST(ub.batch_id AS text)
             AND a.event_type = 'DOCUMENT_COPIED_FROM_DUPLICATE'
            WHERE ub.correlation_id LIKE 'p2-duplicate-merge:%'
              AND a.details->>'job' ~ '^same-file:[0-9a-fA-F-]{36}$'
        ),
        who AS (
            SELECT c.tenant_id, c.batch_id, c.was,
                   COALESCE(
                       (SELECT bu.uploaded_by_actor_id FROM auditcore.p2_bulk_files bf
                          JOIN auditcore.p2_bulk_uploads bu ON bu.tenant_id = bf.tenant_id AND bu.bulk_id = bf.bulk_id
                         WHERE bf.tenant_id = c.tenant_id AND bf.journey_id = c.copy_journey_id
                         ORDER BY bf.created_at_utc LIMIT 1),
                       (SELECT d.pc_actor_id FROM auditcore.p2_journey_deletions d
                         WHERE d.tenant_id = c.tenant_id AND d.journey_id = c.copy_journey_id
                           AND d.pc_actor_id IS NOT NULL
                         ORDER BY d.requested_at_utc DESC LIMIT 1),
                       (SELECT j.created_by_actor_id FROM auditcore.journeys j
                         WHERE j.tenant_id = c.tenant_id AND j.journey_id = c.copy_journey_id)
                   ) AS uploader
            FROM carried c
        )
        UPDATE auditcore.p2_upload_batches ub
           SET uploaded_by_actor_id = w.uploader, updated_at_utc = now()
          FROM who w
         WHERE ub.tenant_id = w.tenant_id AND ub.batch_id = w.batch_id
           AND w.uploader IS NOT NULL AND w.uploader <> ''
           AND ub.uploaded_by_actor_id IS DISTINCT FROM w.uploader
    """)


def downgrade() -> None:
    # Not reversible by value: who an upload was credited to before is not recorded, and
    # crediting the earlier booking's creator again would bring the defect back.
    pass
