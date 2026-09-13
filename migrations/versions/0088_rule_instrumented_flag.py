"""0088_rule_instrumented_flag — mark which AUDIT_CORE rules actually write
to the Execution Log.

Phase 4 of the rule-engine platform instrumented 7 of ~24 AUDIT_CORE rules
(WRONG_DOCUMENT, DUPLICATE_RECEIPT, MANUAL_VERIFICATION, MODEL_NOT_IDENTIFIED,
PAYMENT_BANK_UNMATCHED, AUTOMATED_SYNC_FAILURE, DUPLICATE_BOOKING) to write a
rule_executions row on every PASS/FAIL/SKIPPED outcome. The rest still only
ever produce a row indirectly, by raising an audit_findings row on FAIL --
a clean pass or a not-applicable case leaves zero trace in rule_executions.

Without this flag, the new Compliance Report "Rule Status" tab (Phase 6)
can't tell "genuinely hasn't run yet" (a real PENDING) apart from "we simply
don't record this rule's non-failing outcomes" (a data-coverage gap, not a
pending state) for any AUDIT_CORE rule with zero execution rows. RULE_ENGINE
rows need no such flag -- Phase 2 already writes PASS/FAIL/SKIPPED for every
RULE_ENGINE rule on every phase call, unconditionally.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0088_rule_instrumented_flag"
down_revision = "0087_duplicate_booking"
branch_labels = None
depends_on = None

_INSTRUMENTED_RULE_CODES = (
    "WRONG_DOCUMENT",
    "DUPLICATE_RECEIPT",
    "MANUAL_VERIFICATION",
    "MODEL_NOT_IDENTIFIED",
    "PAYMENT_BANK_UNMATCHED",
    "AUTOMATED_SYNC_FAILURE",
    "DUPLICATE_BOOKING",
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.rule_definitions
            ADD COLUMN IF NOT EXISTS execution_log_instrumented boolean NOT NULL DEFAULT false
            """
        )
    )
    conn.execute(
        text(
            """
            UPDATE auditcore.rule_definitions
            SET execution_log_instrumented = true
            WHERE rule_code = ANY(:codes)
            """
        ),
        {"codes": list(_INSTRUMENTED_RULE_CODES)},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text("ALTER TABLE auditcore.rule_definitions DROP COLUMN IF EXISTS execution_log_instrumented")
    )
