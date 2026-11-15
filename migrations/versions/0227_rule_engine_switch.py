"""0227_rule_engine_switch -- whether a Project's journeys are sent to the Rule Engine (2026-11-15).

Product owner 2026-11-15 (rule review, decision 1): "for rule engine thing.. lets keep it in
audit core for Mahindra Orissa". The Rule Engine checks worth keeping now run in Audit Core
(uc03_document_consistency_rules); the rest are retired for that Project.

``tenant_rule_config.rule_engine_enabled`` is the switch, per Project, on by default so no
other Project changes. While it is off the Journey's Rule Engine units call nothing: every
Rule Engine control is "not applicable", and their open tasks and findings close on the
Journey's next evaluation.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0227_rule_engine_switch"
down_revision = "0226_bank_statements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.tenant_rule_config "
        "ADD COLUMN IF NOT EXISTS rule_engine_enabled boolean NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.tenant_rule_config DROP COLUMN IF EXISTS rule_engine_enabled")
