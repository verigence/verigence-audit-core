"""0228_my_documents_alert -- the alert that tells a PC about their own stuck or failed pages (2026-11-15).

Product owner 2026-11-15: "stuck documents. failed jobs should be intimated to each PC as
notification". MY_DOCUMENTS_ATTENTION (uc03_p2_document_alerts) joins the alert kinds; it
names its one reader in ``payload.audienceActorIds``.
"""
from __future__ import annotations

from alembic import op

revision = "0228_my_documents_alert"
down_revision = "0227_rule_engine_switch"
branch_labels = None
depends_on = None

_KINDS = "'SAME_FILE_UPLOADED', 'DUPLICATE_ACROSS_OUTLETS', 'DUPLICATE_SAME_DEALERSHIP'"


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_tenant_alerts DROP CONSTRAINT IF EXISTS p2_tenant_alerts_alert_kind_check")
    op.execute(
        "ALTER TABLE auditcore.p2_tenant_alerts ADD CONSTRAINT p2_tenant_alerts_alert_kind_check "
        f"CHECK (alert_kind IN ({_KINDS}, 'MY_DOCUMENTS_ATTENTION'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM auditcore.p2_tenant_alerts WHERE alert_kind = 'MY_DOCUMENTS_ATTENTION'")
    op.execute("ALTER TABLE auditcore.p2_tenant_alerts DROP CONSTRAINT IF EXISTS p2_tenant_alerts_alert_kind_check")
    op.execute(
        "ALTER TABLE auditcore.p2_tenant_alerts ADD CONSTRAINT p2_tenant_alerts_alert_kind_check "
        f"CHECK (alert_kind IN ({_KINDS}))"
    )
