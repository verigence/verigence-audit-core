"""0213_accessories_check_switch -- the "accessories fitted are billed" question, off (2026-11-12).

Product owner 2026-11-12: "lets remove the tasks for accessories fitted are billed, we may
introduce it later.. keep a flag somewhere for superadmin, but for now just clear all these
tasks". The question was put to the PC on every Journey with a gate pass or an accessory
invoice on file: 383 open on Mahindra Orissa, 315 of them with the accessory invoice
already uploaded.

``tenant_rule_config.accessories_fitted_check_enabled`` is the switch, per Project, off by
default. While it is off the question is not asked and its open tasks close on the
Journey's next evaluation; a PC's earlier "No, an accessory is not billed" keeps its
finding. Setting it to true for a Project brings the question back.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0213_accessories_check_switch"
down_revision = "0212_source_values_withdrawn"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.tenant_rule_config "
        "ADD COLUMN IF NOT EXISTS accessories_fitted_check_enabled boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.tenant_rule_config DROP COLUMN IF EXISTS accessories_fitted_check_enabled")
