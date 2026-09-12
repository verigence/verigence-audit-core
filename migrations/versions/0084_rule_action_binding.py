"""0084_rule_action_binding — action bound to rule + disposition rename.

Two changes, agreed in the rule-engine platform design session:

1. Disposition taxonomy: rename the adjudication verdict "NOT_A_BREACH" to
   "FALSE_POSITIVE" (clearer, matches industry vocabulary) and the action
   codes that produce it -- ACCEPT -> CONFIRM_BREACH, REJECT ->
   MARK_FALSE_POSITIVE (application-side rename lives in uc03_audit_flags.py
   / uc03_finding_routing.py). Existing OPEN application state (the
   ``disposition`` column) is data-migrated; the append-only
   ``audit_finding_events`` history is left untouched -- an audit trail is
   never rewritten, a historical ACCEPT/REJECT event stays exactly as it
   was recorded.

2. "Action bound to rule" (Phase 1.5 of the rule-engine platform): every
   rule in ``auditcore.rule_definitions`` gets an explicit
   ``bound_actions`` list -- the action verbs meaningful for THIS rule's
   failure, not just inherited from its finding_class -- and a
   ``blocking_completion`` flag, formalizing what was previously only
   implicit in each producer's own ``_machine_flag(blocking_completion=...)``
   call. Both are backfilled from the SAME logic already live in
   ``uc03_finding_routing.py::permitted_actions`` (self-serve ->
   REMARK+RESOLVE; adjudicated -> REMARK+ACKNOWLEDGE+CONFIRM_BREACH+
   MARK_FALSE_POSITIVE+RESOLVE) and, for blocking_completion, the actual
   ``blocking_completion=`` value passed at each of the 25 rules' call
   site (verified by reading every call site -- only PAY_UNVERIFIED_RECEIPT
   passes True today).

Rule-engine rows (RULE_ENGINE executor) are seeded with the adjudicated set
too -- every rule-engine anomaly is VIOLATION/ADJUDICATED per the existing
model -- and blocking_completion=false (none of them block a stage today).
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0084_rule_action_binding"
down_revision = "0083_uc03_rule_definitions"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"

_SELF_SERVICE_ACTIONS = ["REMARK", "RESOLVE"]
_ADJUDICATED_ACTIONS = ["REMARK", "ACKNOWLEDGE", "CONFIRM_BREACH", "MARK_FALSE_POSITIVE", "RESOLVE"]

# rule_code -> blocking_completion, for every row where it's true. Verified
# directly against every _machine_flag(...) call site; every rule not
# listed here passes blocking_completion=False.
_BLOCKING_RULES: frozenset[str] = frozenset({"PAY_UNVERIFIED_RECEIPT"})


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Disposition rename ────────────────────────────────────────────
    # Drop the old constraint FIRST -- it doesn't know FALSE_POSITIVE, so the
    # data UPDATE below would fail against it if run first.
    conn.execute(
        text(
            "ALTER TABLE auditcore.audit_findings "
            "DROP CONSTRAINT IF EXISTS ck_audit_findings_disposition"
        )
    )
    conn.execute(
        text(
            "UPDATE auditcore.audit_findings SET disposition = 'FALSE_POSITIVE' "
            "WHERE disposition = 'NOT_A_BREACH'"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE auditcore.audit_findings "
            "ADD CONSTRAINT ck_audit_findings_disposition "
            "CHECK (disposition IS NULL OR disposition IN "
            "('FIXED','CONFIRMED_BREACH','FALSE_POSITIVE'))"
        )
    )

    # ── 2. Action bound to rule ──────────────────────────────────────────
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.rule_definitions
                ADD COLUMN IF NOT EXISTS bound_actions text[] NOT NULL DEFAULT '{}',
                ADD COLUMN IF NOT EXISTS blocking_completion boolean NOT NULL DEFAULT false
            """
        )
    )

    conn.execute(
        text(
            """
            UPDATE auditcore.rule_definitions
            SET bound_actions = CASE
                    WHEN resolution_mode = 'ADJUDICATED' THEN CAST(:adjudicated AS text[])
                    WHEN resolution_mode = 'SELF_SERVICE' THEN CAST(:self_service AS text[])
                    ELSE '{}'::text[]
                END
            """
        ),
        {"adjudicated": _ADJUDICATED_ACTIONS, "self_service": _SELF_SERVICE_ACTIONS},
    )
    conn.execute(
        text(
            "UPDATE auditcore.rule_definitions SET blocking_completion = true "
            "WHERE rule_code = ANY(:codes)"
        ),
        {"codes": list(_BLOCKING_RULES)},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            "ALTER TABLE auditcore.rule_definitions "
            "DROP COLUMN IF EXISTS bound_actions, "
            "DROP COLUMN IF EXISTS blocking_completion"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE auditcore.audit_findings "
            "DROP CONSTRAINT IF EXISTS ck_audit_findings_disposition"
        )
    )
    conn.execute(
        text(
            "UPDATE auditcore.audit_findings SET disposition = 'NOT_A_BREACH' "
            "WHERE disposition = 'FALSE_POSITIVE'"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE auditcore.audit_findings "
            "ADD CONSTRAINT ck_audit_findings_disposition "
            "CHECK (disposition IS NULL OR disposition IN "
            "('FIXED','CONFIRMED_BREACH','NOT_A_BREACH'))"
        )
    )
