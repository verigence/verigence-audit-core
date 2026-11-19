"""0249_dms_duplicate_lines -- a DMS dump line repeating an earlier line of the same dump
(the same chassis, the same OTF) is DUPLICATE: kept, listed, not counted (2026-11-19: a delivery
sheet carries one chassis on three lines, with three customer names). Rows already uploaded
are set at their next re-match.
"""
from __future__ import annotations

from alembic import op

revision = "0249_dms_duplicate_lines"
down_revision = "0248_dms_name_differs"
branch_labels = None
depends_on = None

_BEFORE = ("MATCHED", "NOT_IN_TOOL", "MISSING", "CANCELLED", "OUT_OF_PERIOD", "EXCLUDED")


def _check(outcomes: tuple[str, ...]) -> None:
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows DROP CONSTRAINT IF EXISTS p2_dms_dump_rows_outcome_check")
    listed = ", ".join(f"'{o}'" for o in outcomes)
    op.execute(f"ALTER TABLE auditcore.p2_dms_dump_rows ADD CONSTRAINT p2_dms_dump_rows_outcome_check "
               f"CHECK (outcome IN ({listed}))")


def upgrade() -> None:
    _check((*_BEFORE, "DUPLICATE"))


def downgrade() -> None:
    op.execute("UPDATE auditcore.p2_dms_dump_rows SET outcome = 'MISSING' WHERE outcome = 'DUPLICATE'")
    _check(_BEFORE)
