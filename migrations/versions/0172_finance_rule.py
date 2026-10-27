"""0172_finance_rule -- a finance record no document supports can be removed (2026-10-27).

Product owner rule: a deal is financed only when the RTO challan, the insurance
hypothecation or a receipt from the financier shows it; cash is never finance.
uc03_finance_rule removes a finance record Audit Core copied from documents once
the Journey's current documents no longer show finance (12 Journeys showed a
loan from "CASH"). Records a person entered are never removed by it.
"""
from __future__ import annotations

from alembic import op

revision = "0172_finance_rule"
down_revision = "0171_platform_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT DELETE ON auditcore.finance_records TO audit_core_runtime")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON auditcore.finance_records FROM audit_core_runtime")
