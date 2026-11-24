"""0260_file_tasks_to_uploader -- a PC's task about one uploaded file goes to its uploader (2026-11-24).

Product owner, 24 Nov 2026: "failed page task to go to the new person". A Drive file joined to a
booking another PC made had its failed page's "Document to resubmit" task left with the booking's
creator. From now on (uc03_p2_task_producer.file_uploader) a PC's task about one file -- a failed
page, a refused page, an unrecognised page, a slow or large upload -- goes to the person who
uploaded that file when it is not the booking's own PC. This hands the tasks already open to
them. A task a person has assigned keeps its assignee; closed tasks are left as they are.
"""
from __future__ import annotations

from alembic import op

revision = "0260_file_tasks_to_uploader"
down_revision = "0259_carried_pages_keep_uploader"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE auditcore.p2_tasks t
           SET assigned_actor_id = b.uploaded_by_actor_id, updated_at_utc = now()
          FROM auditcore.p2_upload_batches b
          JOIN auditcore.journeys j ON j.tenant_id = b.tenant_id AND j.journey_id = b.journey_id
         WHERE t.tenant_id = b.tenant_id AND t.journey_id = b.journey_id
           AND t.reference->>'batchId' = CAST(b.batch_id AS text)
           AND t.origin_kind = 'SYSTEM' AND upper(t.assigned_role_code) = 'PC' AND t.assigned_actor_id IS NULL
           AND t.task_status NOT IN ('VERIFIED_COMPLETE', 'CANCELLED', 'FAILED', 'DEAD_LETTER')
           AND b.uploaded_by_actor_id IS NOT NULL AND b.uploaded_by_actor_id NOT IN ('', 'SYSTEM')
           AND b.uploaded_by_actor_id IS DISTINCT FROM j.created_by_actor_id
    """)


def downgrade() -> None:
    # Not reversible by value: which tasks were unassigned before is not recorded.
    pass
