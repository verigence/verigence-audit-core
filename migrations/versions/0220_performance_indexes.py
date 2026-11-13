"""0220_performance_indexes -- day-window reads of uploads and task events.

Product owner, 13 Nov 2026: "make sure that the DB queries are well optimized, don't
cause cost overheads". The Project Tracker and the Performance view read the uploads and
the task events of a Project between two moments; neither table had an index on
(tenant_id, created_at_utc), so every such read scanned the whole table.
"""
from __future__ import annotations

from alembic import op

revision = "0220_performance_indexes"
down_revision = "0219_list_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_p2_upload_batches_tenant_created "
               "ON auditcore.p2_upload_batches (tenant_id, created_at_utc)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_p2_task_events_tenant_created "
               "ON auditcore.p2_task_events (tenant_id, created_at_utc)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_task_events_tenant_created")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_upload_batches_tenant_created")
