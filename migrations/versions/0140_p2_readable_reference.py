"""0140_p2_readable_reference — a readable reference per Journey.

A Phase 2 Journey now gets OUTLET-PC-YYMMDD-NNN (e.g. UTK-AC-261001-003): the
first three letters of the outlet, two letters for the PC, the IST day, and a
running number for that outlet, PC and day. It is set by the create-booking
API; existing VJ references and every other creation path are untouched.

This only adds the safety net: a reference in the new format can never be
issued twice inside a tenant.
"""
from __future__ import annotations

from alembic import op

revision = "0140_p2_readable_reference"
down_revision = "0139_p2_page_reread_work"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_journeys_readable_reference
          ON auditcore.journeys (tenant_id, journey_reference)
          WHERE journey_reference ~ '^[A-Z]{3}-[A-Z]{2}-[0-9]{6}-[0-9]+$';
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ux_journeys_readable_reference;")
