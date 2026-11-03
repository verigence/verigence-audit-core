"""0188_cross_outlet_alert -- a second Project alert kind (2026-11-03).

Product owner, 2026-11-03: the same customer (PAN and/or Aadhaar), mobile and
model booked at two different outlets -- of one dealership or of two -- is a
duplicate booking across outlets, not a PC's mistake: nothing is deleted and a
large deep-red alert goes to the PCs of both outlets, their Reviewers and Team
Leads (uc03_p2_same_file, uc03_p2_tenant_alerts). The alerts table allowed one
kind only (SAME_FILE_UPLOADED); this allows DUPLICATE_ACROSS_OUTLETS too.
Additive: the existing rows and the unique (tenant, kind, subject journey)
marker are unchanged.
"""
from __future__ import annotations

from alembic import op

revision = "0188_cross_outlet_alert"
down_revision = "0187_same_file_deletion_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_tenant_alerts DROP CONSTRAINT IF EXISTS p2_tenant_alerts_alert_kind_check")
    op.execute(
        "ALTER TABLE auditcore.p2_tenant_alerts ADD CONSTRAINT p2_tenant_alerts_alert_kind_check "
        "CHECK (alert_kind IN ('SAME_FILE_UPLOADED', 'DUPLICATE_ACROSS_OUTLETS'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM auditcore.p2_tenant_alerts WHERE alert_kind = 'DUPLICATE_ACROSS_OUTLETS'")
    op.execute("ALTER TABLE auditcore.p2_tenant_alerts DROP CONSTRAINT IF EXISTS p2_tenant_alerts_alert_kind_check")
    op.execute(
        "ALTER TABLE auditcore.p2_tenant_alerts ADD CONSTRAINT p2_tenant_alerts_alert_kind_check "
        "CHECK (alert_kind = 'SAME_FILE_UPLOADED')"
    )
