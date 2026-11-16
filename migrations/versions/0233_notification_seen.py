"""0233_notification_seen -- when each person last opened their notifications (2026-11-16).

Product owner 2026-11-16: "can we introduce a notification icon and they should click on it
know the status or any notification messages". The bell counts what is new since the person
last opened it; that moment is kept here, per person and Project, so it is the same on the
web and the app (the banner's dismissals were kept in one browser only).
"""
from __future__ import annotations

from alembic import op

revision = "0233_notification_seen"
down_revision = "0232_insurance_dumps"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_notification_seen"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id    varchar(128) NOT NULL,
            actor_id     varchar(160) NOT NULL,
            seen_at_utc  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, actor_id)
        )
        """
    )
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
