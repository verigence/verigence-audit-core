"""0219_list_query_indexes -- three small indexes for the task list and journey list.

Product owner, 13 Nov 2026: fix the slow task-list queries (Neon cost review).
Measured on the live database (Mahindra Orissa, 1,516 journeys):

- The journey list's "stuck" flag (swept_pending_sql) looks for PAGE_SWEPT /
  PAGE_REMOVED activity events. With no index on event_type, each list or count
  read the whole activity log (416,773 events, 239 MB) four times: 490 of the
  533 ms of one call. Only 2,277 events are of those two kinds, so a partial
  index on them is tiny. The worker's own swept-page checks use the same rule.
- Both lists read open tasks only. A partial index on open tasks, carrying the
  columns the lists filter, group and sort on, lets them read the index instead
  of every task row.
- The task list counts each task's comments; a partial index on events that
  carry a comment reads none of a task's other events.
"""
from __future__ import annotations

from alembic import op

revision = "0219_list_query_indexes"
down_revision = "0218_job_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_p2_activity_events_page_marks
          ON auditcore.p2_activity_events (tenant_id, journey_id, subject_id, event_type)
          WHERE event_type IN ('PAGE_SWEPT', 'PAGE_REMOVED')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_p2_tasks_open
          ON auditcore.p2_tasks (tenant_id, journey_id)
          INCLUDE (assigned_role_code, origin_kind, raised_by_actor_id, task_type,
                   due_at_utc, created_at_utc)
          WHERE task_status NOT IN ('VERIFIED_COMPLETE', 'CANCELLED', 'FAILED', 'DEAD_LETTER')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_p2_task_events_comments
          ON auditcore.p2_task_events (tenant_id, task_id)
          WHERE comment IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_task_events_comments")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_tasks_open")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_activity_events_page_marks")
