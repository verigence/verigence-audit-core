"""0196_queue_quality_confirmed -- a page the PC sent again after a scan-quality refusal (2026-11-06).

Product owner: "I have seen pics being correct only ... we have to take the same
page and upload." DI refuses a page whose scan scores fall past its thresholds
(faded, blurred, small), and gives the same verdict for the same bytes every time:
26 pages re-sent in 10 days were all refused again.

``p2_document_queue.quality_confirmed``: set on a page that replaces a copy of
itself (same SHA-256, same Journey) that DI refused for scan quality. Such a page
is sent with DI's UPLOADER_CONFIRMED quality gate: DI records its scores but
reads it. Recorded on the new page because replacing the old copy overwrites
the old copy's refusal code (SUPERSEDED_PAGE). Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0196_queue_quality_confirmed"
down_revision = "0195_project_audit_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.p2_document_queue "
        "ADD COLUMN IF NOT EXISTS quality_confirmed boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_document_queue DROP COLUMN IF EXISTS quality_confirmed")
