"""0134_uc03_p2_other_document_name — a page kept as Others carries the name the PC gave it.

A page Document Intelligence could not classify and the PC keeps as
"Others" stays on file unread. Until now it was shown as "Others" with
nothing to tell one from another; the PC now names it when keeping it
(decision 2026-09-30) and the Upload / Edit card and Journey 360 show
that name.
"""
from __future__ import annotations

from alembic import op

revision = "0134_uc03_p2_other_document_name"
down_revision = "0133_journey_housekeeping_p2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_document_queue ADD COLUMN display_name varchar(120)")


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_document_queue DROP COLUMN display_name")
