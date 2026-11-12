"""0217_unrecognised_message -- the message on a page no document type fits (product owner 2026-11-12).

"If the document fails classification due to schema not available, then we have to send a
message that system doesn't understand the document. Pls recheck and if required submit
again. For issues with quality, continue with same error message." New pages carry the new
wording (uc03_p2_worker.UNRECOGNISED_REASON); this rewrites the pages already showing the old
one. Open "Set the document type" tasks take the new description on their next evaluation.
Only the text changes; downgrade puts the old text back.
"""
from __future__ import annotations

from alembic import op

revision = "0217_unrecognised_message"
down_revision = "0216_drive_access"
branch_labels = None
depends_on = None

_OLD = "Kept as supporting evidence. Set the document type if this is a checklist document."
_NEW = "The system does not understand this document. Please recheck it and, if required, submit it again."


def upgrade() -> None:
    op.execute(
        f"UPDATE auditcore.p2_document_queue SET status_reason='{_NEW}', updated_at_utc=now() "
        f"WHERE status_reason='{_OLD}'"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE auditcore.p2_document_queue SET status_reason='{_OLD}', updated_at_utc=now() "
        f"WHERE status_reason='{_NEW}'"
    )
