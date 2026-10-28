"""0176_retire_ndc_signature -- no question about the NDC signature (2026-10-28).

Product owner: "NDC signed in our presence is not mandatory and no question for
it". The two checks NDC_SIGNATURE_UNCONFIRMED (the PC's question) and
NDC_NOT_SIGNED (the Team Lead's finding) are removed from the catalogue; this
migration closes what they left behind: the PC's open question tasks are
cancelled (with the reason on the task) and their check rows are removed, so no
screen shows a check that no longer runs. Findings, if any, are untouched: an
audit trail is never rewritten.
"""
from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

revision = "0176_retire_ndc_signature"
down_revision = "0175_work_queue_order"
branch_labels = None
depends_on = None

_CODES = ["NDC_SIGNATURE_UNCONFIRMED", "NDC_NOT_SIGNED"]


def upgrade() -> None:
    _retire(op.get_bind())


def _retire(conn) -> None:
    conn.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status='CANCELLED',
                completion_result=COALESCE(completion_result, '{}'::jsonb) || CAST(:result AS jsonb),
                updated_at_utc=now()
            WHERE source_code = ANY(:codes)
              AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
            """
        ),
        {"codes": _CODES, "result": json.dumps({
            "outcome": "CHECK_RETIRED",
            "reason": "The NDC signature is no longer asked about (product owner 2026-10-28).",
        })},
    )
    conn.execute(text("DELETE FROM auditcore.p2_control_state WHERE control_code = ANY(:codes)"), {"codes": _CODES})


def downgrade() -> None:
    # The checks come back with the code that runs them; they rewrite their own rows.
    pass
