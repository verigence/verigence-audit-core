"""0248_dms_name_differs -- a DMS dump row matched to a Journey whose customer name is not
the dump's (product owner 2026-11-19: "pls update the report" -- the matches where the
chassis, booking number or mobile agree but the customer does not are listed for review).

- journey_customer   the Journey's customer as Verigence holds it, at the match.
- name_agrees        whether the dump's customer is one of the Journey's names; NULL when
                     the row is not matched or prints no name.
"""
from __future__ import annotations

from alembic import op

revision = "0248_dms_name_differs"
down_revision = "0247_dms_excluded_models"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows ADD COLUMN IF NOT EXISTS journey_customer varchar(300)")
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows ADD COLUMN IF NOT EXISTS name_agrees boolean")


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows DROP COLUMN IF EXISTS name_agrees")
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows DROP COLUMN IF EXISTS journey_customer")
