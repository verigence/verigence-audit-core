"""0263_gate_register_sunday -- the gate out register duty is asked for Sunday too (2026-11-24).

Product owner rule of 16 Nov 2026: Sunday is a normal day with its own gate out register (a
missing Sunday register is incomplete like any other day). The standard duty has every day
(daily_duties.STANDARD_DUTIES), but the Project's duty was switched on with Monday to Saturday,
so no item was made for Sunday's register. Sunday is added to the gate out register duties;
the item still shows on the PC's next working day (Monday), as a previous-day duty does.
"""
from __future__ import annotations

from alembic import op

revision = "0263_gate_register_sunday"
down_revision = "0262_dms_dump_row_issues"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE auditcore.p2_duties
        SET weekdays = (SELECT array_agg(DISTINCT w ORDER BY w) FROM unnest(weekdays || CAST(ARRAY[7] AS smallint[])) AS w)
        WHERE standard_code = 'GATE_REGISTER' AND NOT (7 = ANY(weekdays))
    """)


def downgrade() -> None:
    pass  # Sunday's register stays asked for: it is the product owner's rule.
