"""0125_uc03_p2_group_index_merged — a retired (MERGED) group no longer
blocks a new document over the same pages (PC re-typing a page)."""
from __future__ import annotations

from alembic import op

revision = "0125_uc03_p2_group_index_merged"
down_revision = "0124_uc03_p2_control_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS auditcore.ux_p2_document_queue_group;
        CREATE UNIQUE INDEX ux_p2_document_queue_group
          ON auditcore.p2_document_queue(tenant_id, batch_id, page_numbers)
          WHERE unit_kind='GROUP' AND queue_status NOT IN ('CANCELLED','MERGED');
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS auditcore.ux_p2_document_queue_group;
        CREATE UNIQUE INDEX ux_p2_document_queue_group
          ON auditcore.p2_document_queue(tenant_id, batch_id, page_numbers)
          WHERE unit_kind='GROUP' AND queue_status <> 'CANCELLED';
        """
    )
