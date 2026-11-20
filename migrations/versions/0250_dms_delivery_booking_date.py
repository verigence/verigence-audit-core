"""DMS delivery rows: the booking date the sheet prints, where the upload left it empty.

Revision ID: 0250_dms_delivery_booking_date
Revises: 0249_dms_duplicate_lines

Utkal's delivery sheets of 19 and 20 Nov 2026 (603 rows) kept BOOKING DATE in the raw row but
not in booking_date: the sheet heads two columns BOOKING DATE and the empty one was read. The
Deal Type rule then went by the VIN alone and marked bookings of August Inscope (product owner
2026-11-20). The parser now reads the column with values; the stored rows take it from the raw
row. Their Deal Type is decided again by the DMS_REMATCH job.
"""
from __future__ import annotations

from alembic import op

revision = "0250_dms_delivery_booking_date"
down_revision = "0249_dms_duplicate_lines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE auditcore.p2_dms_dump_rows
        SET booking_date = CAST(left(raw->>'BOOKING DATE', 10) AS date)
        WHERE dump_kind = 'DELIVERY' AND booking_date IS NULL AND raw ? 'BOOKING DATE'
          AND raw->>'BOOKING DATE' ~ '^\\d{4}-\\d{2}-\\d{2}'
        """
    )


def downgrade() -> None:
    pass
