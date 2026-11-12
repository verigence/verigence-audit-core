"""0216_drive_access -- each PC's own Google account is given the folders of their outlets (product owner 2026-11-12).

"PC can be given access to multiple folders if possible" and, offered the choice between
one shared login and a folder shared with each PC, "I need option B": the PCs do not hold
the password of the Project's Google account. Verigence shares each outlet's folder with
the Google account of every PC mapped to that outlet, and takes the sharing away when the
PC is no longer mapped.

- p2_drive_people  the Google account a person uses for Drive (their Verigence sign-in
                   email; "each PC's login email is a Google account").
- p2_drive_shares  which outlet folder is shared with whom: Google's permission id (to take
                   it away again), or why Google refused.
"""
from __future__ import annotations

from alembic import op

revision = "0216_drive_access"
down_revision = "0215_drive_schedule"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLES = ("p2_drive_people", "p2_drive_shares")


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE auditcore.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON auditcore.{table}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{table} ON auditcore.{table} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{table} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON auditcore.{table} FROM {_RUNTIME_ROLE}")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_drive_people (
            tenant_id       varchar(128) NOT NULL,
            actor_id        varchar(200) NOT NULL,
            google_email    varchar(320) NOT NULL,
            asked_at_utc    timestamptz  NOT NULL DEFAULT now(),
            updated_at_utc  timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, actor_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_drive_shares (
            tenant_id        varchar(128) NOT NULL,
            drive_folder_id  varchar(128) NOT NULL,
            actor_id         varchar(200) NOT NULL,
            outlet_id        uuid,
            google_email     varchar(320) NOT NULL,
            permission_id    varchar(200),
            last_error       varchar(500),
            shared_at_utc    timestamptz,
            removed_at_utc   timestamptz,
            created_at_utc   timestamptz  NOT NULL DEFAULT now(),
            updated_at_utc   timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, drive_folder_id, actor_id)
        )
        """
    )
    for table in _TABLES:
        _secure(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS auditcore.{table}")
