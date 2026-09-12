"""0087_duplicate_booking — register the native duplicate-booking rule.

DUPLICATE_BOOKING (uc03_duplicate_booking_detection.py) -- a native
audit-core rule replacing the rule-engine's own CROSS_CASE
DUPLICATE_PAN_ACROSS_BOOKINGS / DUPLICATE_AADHAAR_ACROSS_BOOKINGS
mechanism, which is parked (rule-engine migration 0004; no cross-journey
finding-materialization pattern exists there, and audit-core never called
it). This rule lives entirely in audit-core: exact PAN/Aadhaar match, or a
fuzzy KYC-name match combined with a matching address pincode, flags a
likely duplicate; whichever journey's minimum booking amount is confirmed
(or, failing that, whichever was created first) is treated as the
original, and the finding is raised on the other one.

VIOLATION/CRITICAL/TL-adjudicated, matching the severity and adjudication
model the rule-engine's own duplicate-detection rules always carried.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0087_duplicate_booking"
down_revision = "0086_retire_pay_unverified"
branch_labels = None
depends_on = None

_ADJUDICATED_ACTIONS = ["REMARK", "ACKNOWLEDGE", "CONFIRM_BREACH", "MARK_FALSE_POSITIVE", "RESOLVE"]


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            INSERT INTO auditcore.rule_definitions (
                rule_code, category, title, description,
                executor, execution_kind, trigger_events, rerun_policy,
                finding_class, default_severity, default_owner_role,
                resolution_mode, bound_actions, blocking_completion, enabled
            ) VALUES (
                'DUPLICATE_BOOKING', 'Customer & Dealer Identity',
                'Possible duplicate booking for the same customer',
                'The same customer (exact PAN/Aadhaar, or a fuzzy name match combined '
                'with a matching address pincode) appears on more than one booking in '
                'this tenant. Raised on whichever booking is believed to be the '
                'duplicate -- the one whose minimum booking amount is not yet '
                'confirmed, or the later-created one if neither/both are.',
                'AUDIT_CORE', 'CODE', ARRAY['DOCUMENT_SYNCED'], 'RERUNNABLE',
                'VIOLATION', 'CRITICAL', 'TL',
                'ADJUDICATED', :bound_actions, false, true
            )
            ON CONFLICT (rule_code) DO NOTHING
            """
        ),
        {"bound_actions": _ADJUDICATED_ACTIONS},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DELETE FROM auditcore.rule_definitions WHERE rule_code = 'DUPLICATE_BOOKING'"))
