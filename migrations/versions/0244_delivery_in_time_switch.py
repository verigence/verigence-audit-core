"""0244_delivery_in_time_switch -- "Delivery not completed in time", off (2026-11-19).

Product owner 2026-11-19: "for the time being supress the tasks for delay in delivery --
delivery not completed in time.. its not relevant as of now, may we introduce it later".
951 such Team Lead tasks were open on Mahindra Orissa.

``tenant_rule_config.delivery_in_time_check_enabled`` is the switch, per Project, off by
default (as with the accessories-fitted question, 0213). While it is off the check passes,
so its open tasks close on the Journey's next evaluation and the seventh-day event is not
queued. Setting it to true for a Project brings the check back.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0244_delivery_in_time_switch"
down_revision = "0243_correction_on_every_copy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.tenant_rule_config "
        "ADD COLUMN IF NOT EXISTS delivery_in_time_check_enabled boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.tenant_rule_config DROP COLUMN IF EXISTS delivery_in_time_check_enabled")
