"""0203_booking_date_source -- where a journey's booking date came from (2026-11-07).

Product owner, 2026-11-07 20:47 IST: "there should be date attached ... if
booking form date is missing fall back is the audit opening date" -- the day
the PC uploaded the booking documents, else the day the journey was started.
Every journey now carries a booking date (uc03_booking_date), and the booking
record says which rule step gave it:

- BOOKING_FORM            the booking form's booking date (as read or corrected)
- BOOKING_DOCKET          the booking docket's booking date (no booking form)
- ENTERED                 a date a person entered on the booking
- BOOKING_DOCS_UPLOADED   the day the PC first uploaded booking documents (IST)
- JOURNEY_CREATED         the day the journey was started (IST)

The last two are fallbacks for lists and reports only: the deal is never
priced on them and no check reads them as a booking date.

Additive. Existing rows get their source (and, where the rule says so, their
date) from the worker's start-up step (uc03_booking_date.fill_missing_booking_dates),
which applies the same rule as every later recompute -- one implementation of
it rather than a second one in SQL here.
"""
from __future__ import annotations

from alembic import op

revision = "0203_booking_date_source"
down_revision = "0202_gate_pass_past_dates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.bookings ADD COLUMN IF NOT EXISTS booking_date_source varchar(32)")
    op.execute("ALTER TABLE auditcore.bookings DROP CONSTRAINT IF EXISTS ck_bookings_booking_date_source")
    op.execute(
        "ALTER TABLE auditcore.bookings ADD CONSTRAINT ck_bookings_booking_date_source CHECK ("
        "booking_date_source IS NULL OR booking_date_source IN "
        "('BOOKING_FORM', 'BOOKING_DOCKET', 'ENTERED', 'BOOKING_DOCS_UPLOADED', 'JOURNEY_CREATED'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.bookings DROP CONSTRAINT IF EXISTS ck_bookings_booking_date_source")
    op.execute("ALTER TABLE auditcore.bookings DROP COLUMN IF EXISTS booking_date_source")
