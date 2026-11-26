"""Whose Google storage a Drive file takes, and whether it is still in Drive (2026-11-26).

In a plain Google account a file counts against the storage of its owner -- whoever put
it in Drive -- not the owner of the folder. Since PCs upload into their outlet's folder
with their own Google account ("option B", 12 Nov), every file they put in uses their
own 15 GB. Product owner 26 Nov ("lets go with 2"): Verigence keeps its own copy of
every file it takes, so it tells each PC which of their Drive files are safe to delete.

- owner_email              the Google account that owns the file (Drive's owners[0])
- gone_from_drive_at_utc   when the file was seen to be gone from Drive (binned or deleted)
- drive_checked_at_utc     when Verigence last asked Drive about the file
"""
from __future__ import annotations

from alembic import op

revision = "0264_drive_file_owner"
down_revision = "0263_gate_register_sunday"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_drive_files
            ADD COLUMN IF NOT EXISTS owner_email varchar(320),
            ADD COLUMN IF NOT EXISTS gone_from_drive_at_utc timestamptz,
            ADD COLUMN IF NOT EXISTS drive_checked_at_utc timestamptz
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_drive_files_owner ON auditcore.p2_drive_files "
        "(tenant_id, lower(owner_email)) WHERE gone_from_drive_at_utc IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_drive_files_owner")
    op.execute(
        """
        ALTER TABLE auditcore.p2_drive_files
            DROP COLUMN IF EXISTS drive_checked_at_utc,
            DROP COLUMN IF EXISTS gone_from_drive_at_utc,
            DROP COLUMN IF EXISTS owner_email
        """
    )
