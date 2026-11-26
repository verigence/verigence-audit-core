"""Whose Google storage a Drive file takes, and whether it is still in Drive (2026-11-26).

In a plain Google account a file counts against the storage of its owner -- whoever put
it in Drive -- not the owner of the folder. Since PCs upload into their outlet's folder
with their own Google account ("option B", 12 Nov), every file they put in uses their
own 15 GB. Product owner 26 Nov ("lets go with 2"): Verigence keeps its own copy of
every file it takes, so it tells each PC which of their Drive files are safe to delete.

- owner_email              the Google account that owns the file (Drive's owners[0])
- gone_from_drive_at_utc   when the file was seen to be gone from Drive (binned or deleted)
- drive_checked_at_utc     when Verigence last asked Drive about the file
- handed_over_at_utc       when its ownership was moved to the Project's account
- handover_error           why the last move was refused

Same day, "lets go with b) ... PC dont need to do anything externally, from verigence App
itself they should be able to free up their storage, and files are retained in JBR space":
a PC approves once, on Google's own screen from Verigence, and Verigence then moves the
ownership of each file it has taken to the Project's account (the file stays where it is).
p2_drive_people keeps that approval:
- refresh_token_sealed     the PC's refresh token, sealed as the Project's is
- linked_google_email      the Google account the PC approved with
- linked_at_utc, link_status (LINKED / LOST), link_error
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
            ADD COLUMN IF NOT EXISTS drive_checked_at_utc timestamptz,
            ADD COLUMN IF NOT EXISTS handed_over_at_utc timestamptz,
            ADD COLUMN IF NOT EXISTS handover_error text
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.p2_drive_people
            ADD COLUMN IF NOT EXISTS refresh_token_sealed text,
            ADD COLUMN IF NOT EXISTS linked_google_email varchar(320),
            ADD COLUMN IF NOT EXISTS linked_at_utc timestamptz,
            ADD COLUMN IF NOT EXISTS link_status varchar(12)
                CHECK (link_status IS NULL OR link_status IN ('LINKED', 'LOST')),
            ADD COLUMN IF NOT EXISTS link_error text
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
        ALTER TABLE auditcore.p2_drive_people
            DROP COLUMN IF EXISTS link_error, DROP COLUMN IF EXISTS link_status,
            DROP COLUMN IF EXISTS linked_at_utc, DROP COLUMN IF EXISTS linked_google_email,
            DROP COLUMN IF EXISTS refresh_token_sealed
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.p2_drive_files
            DROP COLUMN IF EXISTS handover_error,
            DROP COLUMN IF EXISTS handed_over_at_utc,
            DROP COLUMN IF EXISTS drive_checked_at_utc,
            DROP COLUMN IF EXISTS gone_from_drive_at_utc,
            DROP COLUMN IF EXISTS owner_email
        """
    )
