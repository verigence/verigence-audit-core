"""0166_gate_pass_photo_quality -- the gate pass photo is always kept; its
quality is judged after the upload (product owner 2026-10-27: no hard rules
at the camera; check blur, clarity and crispness in the backend).

Additive: ``p2_gate_passes.quality_flag`` (GOOD, BLURRED, TOO_DARK,
WASHED_OUT) and ``brightness`` beside the existing ``sharpness``.
"""
from __future__ import annotations

from alembic import op

revision = "0166_gate_pass_photo_quality"
down_revision = "0165_activity_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_gate_passes
          ADD COLUMN IF NOT EXISTS quality_flag varchar(32),
          ADD COLUMN IF NOT EXISTS brightness numeric(6, 2);
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_gate_passes DROP COLUMN IF EXISTS quality_flag, "
               "DROP COLUMN IF EXISTS brightness")
