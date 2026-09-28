"""0131_uc03_p2_journey_pc_name — who started the Journey, by name.

A Team Lead scanning Booking & Delivery wants to see which Process
Coordinator each journey belongs to. Audit Core only knows the Security
actor id; the person's display name lives in Security, which a TL cannot
query. The PC's app sends the name it shows for the signed-in user when
it starts a booking, and it is kept here as a snapshot next to the actor
id. Display only: authority stays with ``created_by_actor_id``.
"""
from __future__ import annotations

from alembic import op

revision = "0131_uc03_p2_journey_pc_name"
down_revision = "0130_uc03_p2_live_notify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.journeys "
        "ADD COLUMN IF NOT EXISTS created_by_display_name varchar(200)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.journeys DROP COLUMN IF EXISTS created_by_display_name")
