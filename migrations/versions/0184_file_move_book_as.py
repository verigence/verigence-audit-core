"""0184_file_move_book_as -- a moved file's new booking under another PC of the outlet (2026-11-01).

A file planned for a new booking (p2_file_moves, NEW_BOOKING) is booked for
the PC who uploaded it. When that person is no longer a PC of the outlet
(ADI-BA-261019-004: uploaded by a PC who is now a Reviewer), the operator
names the PC to book it under (product owner, 1 Nov 2026): book_as_actor_id
and the name shown for them, book_as_name. The original uploader stays in
the bookings' history.
"""
from __future__ import annotations

from alembic import op

revision = "0184_file_move_book_as"
down_revision = "0183_file_moves"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_file_moves
          ADD COLUMN IF NOT EXISTS book_as_actor_id varchar(200),
          ADD COLUMN IF NOT EXISTS book_as_name varchar(200)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_file_moves
          DROP COLUMN IF EXISTS book_as_actor_id,
          DROP COLUMN IF EXISTS book_as_name
        """
    )
