"""0189_page_fingerprint_index -- find a page by its fingerprint across a Project (2026-11-03).

A page's fingerprint (``p2_document_queue.page_sha256``, the SHA-256 of the
split page) is now looked up across the whole Project, not within one Journey:
the Daily processing report counts each PC's exact duplicate files (every page
already uploaded before; uc03_p2_daily_report.exact_duplicate_batches), and the
same-file rule carries only the pages an earlier Journey lacks
(uc03_p2_same_file.carry_plan). This index keeps those lookups to a probe per
page. Additive; no data changes.
"""
from __future__ import annotations

from alembic import op

revision = "0189_page_fingerprint_index"
down_revision = "0188_cross_outlet_alert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_document_queue_page_sha256 "
        "ON auditcore.p2_document_queue (tenant_id, page_sha256) "
        "WHERE unit_kind = 'PAGE' AND page_sha256 IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_document_queue_page_sha256")
