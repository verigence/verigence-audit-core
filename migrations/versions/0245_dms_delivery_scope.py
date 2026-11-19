"""0245_dms_delivery_scope -- each DMS delivery dump row gets the Deal Type rule's Inscope /
Outscope (product owner 2026-11-19: "apply the rule of inscope and outscope as we did it in
the verigence for the delivery files and mark them in scope/outscope").

- booking_date      the booking date a delivery report prints (Utkal: BOOKING DATE).
- deal_type         INSCOPE / OUTSCOPE / MR / LEASE / BULK, or NULL (no readable chassis yet).
- deal_type_basis   what it was decided on, in words.

A delivery report's BOOKING column (Aditya: B-13421434) is its booking number; rows already
uploaded get it, and the booking date, from the file as kept (raw). The rule itself runs at
every match (uc03_p2_dms_dumps.match_dump), so the next re-match fills deal_type.
"""
from __future__ import annotations

from alembic import op

revision = "0245_dms_delivery_scope"
down_revision = "0244_delivery_in_time_switch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows ADD COLUMN IF NOT EXISTS booking_date date")
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows ADD COLUMN IF NOT EXISTS deal_type varchar(10)")
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows ADD COLUMN IF NOT EXISTS deal_type_basis text")
    op.execute(
        """
        UPDATE auditcore.p2_dms_dump_rows
        SET booking_number = left(raw->>'BOOKING', 60)
        WHERE dump_kind = 'DELIVERY' AND booking_number IS NULL AND raw ? 'BOOKING'
          AND raw->>'BOOKING' ~ '^[A-Za-z0-9/-]{4,}$'
        """
    )
    op.execute(
        """
        UPDATE auditcore.p2_dms_dump_rows
        SET booking_date = CAST(left(raw->>'BOOKING DATE', 10) AS date)
        WHERE dump_kind = 'DELIVERY' AND booking_date IS NULL AND raw ? 'BOOKING DATE'
          AND raw->>'BOOKING DATE' ~ '^\\d{4}-\\d{2}-\\d{2}'
        """
    )


def downgrade() -> None:
    for column in ("deal_type_basis", "deal_type", "booking_date"):
        op.execute(f"ALTER TABLE auditcore.p2_dms_dump_rows DROP COLUMN IF EXISTS {column}")
