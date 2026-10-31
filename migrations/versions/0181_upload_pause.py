"""0181_upload_pause -- "Uploads paused for maintenance" switch (2026-10-31).

Product owner, month end: while bookings that hold other customers' files are
corrected, PCs must not upload documents, but they still open bookings, read
everything and work their tasks.

``p2_upload_pause`` holds at most one row per scope: a Project's own row
(tenant_id) or the all-Projects row (tenant_id NULL), kept unique on
coalesce(tenant_id, '*'). Uploads are refused while a row that covers the
Project says paused and its until_utc is unset or still ahead
(uc03_p2_upload_pause.upload_pause). No row, or paused = false, changes
nothing.

Row security: every Project may read its own row and the all-Projects row
(the upload endpoints and the status read); only a SuperAdmin request
(app.platform_super_admin, set after Security's attestation) writes.
"""
from __future__ import annotations

from alembic import op

revision = "0181_upload_pause"
down_revision = "0180_worker_churn"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_upload_pause"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            pause_id         uuid         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id        varchar(128),
            paused           boolean      NOT NULL,
            message          text,
            until_utc        timestamptz,
            set_by_actor_id  varchar(160),
            set_at_utc       timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{_TABLE}_scope "
        f"ON auditcore.{_TABLE} ((COALESCE(tenant_id, '*')))"
    )
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_read ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY {_TABLE}_read ON auditcore.{_TABLE} FOR SELECT "
        "USING (tenant_id IS NULL OR tenant_id = auditcore.current_tenant_id() "
        "OR auditcore.current_platform_super_admin())"
    )
    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_admin_insert ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY {_TABLE}_admin_insert ON auditcore.{_TABLE} FOR INSERT "
        "WITH CHECK (auditcore.current_platform_super_admin())"
    )
    op.execute(f"DROP POLICY IF EXISTS {_TABLE}_admin_update ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY {_TABLE}_admin_update ON auditcore.{_TABLE} FOR UPDATE "
        "USING (auditcore.current_platform_super_admin()) "
        "WITH CHECK (auditcore.current_platform_super_admin())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON auditcore.{_TABLE} FROM {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
