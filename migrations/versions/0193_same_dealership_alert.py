"""0193_same_dealership_alert -- a third Project alert kind (2026-11-04).

Product owner, 2026-11-04: the same customer (PAN and/or Aadhaar), mobile and
model at two outlets of the SAME dealership pauses the uploads of the PCs of
both bookings until a Reviewer deletes the duplicate
(DUPLICATE_SAME_DEALERSHIP, uc03_p2_same_file, uc03_p2_upload_pause); at two
dealerships it stays DUPLICATE_ACROSS_OUTLETS, now on every screen. Additive.
"""
from __future__ import annotations

from alembic import op

revision = "0193_same_dealership_alert"
down_revision = "0192_reviewer_journey_closures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.p2_tenant_alerts DROP CONSTRAINT IF EXISTS p2_tenant_alerts_alert_kind_check")
    op.execute(
        "ALTER TABLE auditcore.p2_tenant_alerts ADD CONSTRAINT p2_tenant_alerts_alert_kind_check "
        "CHECK (alert_kind IN ('SAME_FILE_UPLOADED', 'DUPLICATE_ACROSS_OUTLETS', 'DUPLICATE_SAME_DEALERSHIP'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM auditcore.p2_tenant_alerts WHERE alert_kind = 'DUPLICATE_SAME_DEALERSHIP'")
    op.execute("ALTER TABLE auditcore.p2_tenant_alerts DROP CONSTRAINT IF EXISTS p2_tenant_alerts_alert_kind_check")
    op.execute(
        "ALTER TABLE auditcore.p2_tenant_alerts ADD CONSTRAINT p2_tenant_alerts_alert_kind_check "
        "CHECK (alert_kind IN ('SAME_FILE_UPLOADED', 'DUPLICATE_ACROSS_OUTLETS'))"
    )
