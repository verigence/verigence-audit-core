"""0085_uc03_rule_executions — unified rule Execution Log.

Phase 2 of the unified rule-engine platform: one row per rule per trigger
invocation, whatever the outcome (PASS / FAIL / SKIPPED / ERROR) -- the
piece neither engine had before this platform: audit-core's own rules only
ever wrote something when they FAILED (a row in audit_findings); the
rule-engine's PASS/FAIL/SKIPPED discipline existed in its own audit_runs
table but was never relayed back into audit-core, so nothing here could
answer "did this rule run, and what happened" in one place.

``rule_code`` is deliberately NOT a foreign key to auditcore.rule_definitions:
RULE_ENGINE-executor rows are fetched live and never persisted into
rule_definitions (Phase 1's design), so a hard FK would reject every
RULE_ENGINE-originated execution row. It stores the bare rule code either
way (e.g. "PRICE_BOOKING_VS_INVOICE", not the audit_findings.rule_key
convention's "RE_PRICE_BOOKING_VS_INVOICE" prefix) so it lines up with
GET /rule-catalog's ruleCode for both executors.

This migration is additive and creates no data -- Phase 2's code change
(wiring uc03_rule_engine_findings.py's run_rule_engine_phase to write rows
here via the rule-engine's own readiness() response) starts populating it
for RULE_ENGINE rows. AUDIT_CORE producer instrumentation is Phase 4,
scoped separately.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0085_uc03_rule_executions"
down_revision = "0084_rule_action_binding"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS auditcore.rule_executions (
                rule_execution_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id          varchar(120) NOT NULL,
                journey_id         uuid NOT NULL,
                rule_code          varchar(160) NOT NULL,
                triggering_event   varchar(60) NOT NULL,
                outcome            varchar(20) NOT NULL
                                   CHECK (outcome IN ('PASS','FAIL','SKIPPED','ERROR')),
                reason             text,
                audit_finding_id   uuid,
                evaluated_at_utc   timestamptz NOT NULL DEFAULT now(),
                correlation_id     varchar(120),
                CONSTRAINT fk_rule_executions_audit_finding
                    FOREIGN KEY (tenant_id, audit_finding_id)
                    REFERENCES auditcore.audit_findings (tenant_id, audit_finding_id)
            )
            """
        )
    )
    # "Has this ONCE rule already run for this journey" (Phase 3) and "history
    # for this rule on this journey" (Phase 5 drill-through) are the same
    # access pattern -- most-recent-first per (tenant, journey, rule_code).
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_rule_executions_journey_rule
            ON auditcore.rule_executions (tenant_id, journey_id, rule_code, evaluated_at_utc DESC)
            """
        )
    )
    # Noisy-rule feedback loop (explicitly deferred, kept in the design):
    # rate of FAIL executions per rule whose finding later became a
    # FALSE_POSITIVE disposition is a join through audit_finding_id -- this
    # index is what makes "every FAIL for rule X" cheap to aggregate later.
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_rule_executions_rule_outcome
            ON auditcore.rule_executions (tenant_id, rule_code, outcome)
            """
        )
    )
    conn.execute(
        text(f"GRANT SELECT, INSERT ON auditcore.rule_executions TO {_RUNTIME_ROLE}")
    )
    conn.execute(
        text(f"REVOKE UPDATE, DELETE ON auditcore.rule_executions FROM {_RUNTIME_ROLE}")
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TABLE IF EXISTS auditcore.rule_executions CASCADE"))
