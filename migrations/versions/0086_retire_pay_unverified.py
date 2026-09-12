"""0086_retire_pay_unverified_receipt — consolidate onto PAYMENT_BANK_UNMATCHED.

PAY_UNVERIFIED_RECEIPT (raised once, at DELIVERY_COMPLETED, checking for a
captured payment with no VERIFIED payment_verification_events row) and
PAYMENT_BANK_UNMATCHED (raised continuously on DOCUMENT_SYNCED, checking the
exact same underlying fact -- _record_verification is the only path that
ever writes a VERIFIED event, and it's called from the same
reconcile_payments producer PAYMENT_BANK_UNMATCHED already owns) were two
separate first-class rules for one real-world check. Confirmed by reading
both producers directly, not assumed.

uc03_delivery_commands.py's own unverified-payment gap check now raises
under PAYMENT_BANK_UNMATCHED's own per-payment rule_key instead of a
separate PAY_UNVERIFIED_RECEIPT rule_key (application code change, shipped
alongside this migration) -- this migration retires the now-unused
Definition row so the unified catalog shows one rule, not two.

Historical audit_findings rows already raised under the old
PAY_UNVERIFIED_RECEIPT rule_key are untouched (an audit trail is never
rewritten) and still classify correctly -- uc03_finding_routing.py's
classification tables keep recognizing that rule_key.

PAYMENT_BANK_UNMATCHED's blocking_completion is set true: the retired
rule's one distinguishing property (this check can legitimately hold up
Delivery completion) transfers to the surviving rule, which now has a call
site (the former PAY_UNVERIFIED_RECEIPT one) that raises it with
blocking_completion=true.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0086_retire_pay_unverified"
down_revision = "0085_uc03_rule_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text("DELETE FROM auditcore.rule_definitions WHERE rule_code = 'PAY_UNVERIFIED_RECEIPT'")
    )
    conn.execute(
        text(
            "UPDATE auditcore.rule_definitions SET blocking_completion = true "
            "WHERE rule_code = 'PAYMENT_BANK_UNMATCHED'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            "UPDATE auditcore.rule_definitions SET blocking_completion = false "
            "WHERE rule_code = 'PAYMENT_BANK_UNMATCHED'"
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO auditcore.rule_definitions (
                rule_code, category, title, description,
                executor, execution_kind, trigger_events, rerun_policy,
                finding_class, default_severity, default_owner_role,
                resolution_mode, bound_actions, blocking_completion, enabled
            ) VALUES (
                'PAY_UNVERIFIED_RECEIPT', 'Delivery Process', 'Delivery completed with unverified payment',
                'One or more captured payments do not have a VERIFIED realization status at Delivery completion.',
                'AUDIT_CORE', 'CODE', ARRAY['DELIVERY_COMPLETED'], 'ONCE',
                'DATA_GAP', 'HIGH', 'PC',
                'SELF_SERVICE', ARRAY['REMARK','RESOLVE'], true, true
            )
            ON CONFLICT (rule_code) DO NOTHING
            """
        )
    )
