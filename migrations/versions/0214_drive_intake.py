"""0214_drive_intake -- journey files dropped in the Project's Google Drive (product owner 2026-11-12).

PCs put a customer's files in Google Drive, under the Project's own Google account;
Verigence takes them from there (uc03_p2_drive_intake). Drive is the inbox, these
tables are the record:

- p2_drive_connections  one per Project: the Google account it is connected as (the
                        refresh token sealed), the Project folder, and where the
                        account's change feed was last read.
- p2_drive_folders      every folder of the tree, by its Drive id: the ones Verigence
                        makes (Project / dealership / outlet, a folder per PC where an
                        outlet has more than one, and the archive) and the ones a PC makes
                        (a customer's folder "Name-Number", and what is inside it). A
                        customer's folder keeps the journey its files went to, so a file
                        added to it later goes to the same journey; once the journey has
                        been closed for the Project's retention period the folder is moved
                        to the archive (never deleted).
- p2_drive_files        every file seen, by its Drive id and content (MD5): what it is,
                        whose folder it is in, when it was put there, and what became of
                        it -- waiting to be taken, taken (its copy in storage and the
                        bulk upload file or photo it became), set aside, or failed.
- p2_drive_runs         every time the files were taken: who asked (a PC's submit, the
                        end of the day, a Team Lead / PM / Reviewer on demand), for which
                        outlet and day of upload, and what it did.

A Drive file is read through the bulk upload (0190): p2_bulk_uploads says where a drop
came from, and p2_bulk_files may name the journey a file belongs to already (its
customer's folder went there before) and the booking number its folder gives.

Tenant row security as every Audit Core table; the runtime role may read, add and
update, never delete.
"""
from __future__ import annotations

from alembic import op

revision = "0214_drive_intake"
down_revision = "0213_accessories_check_switch"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLES = ("p2_drive_connections", "p2_drive_folders", "p2_drive_files", "p2_drive_runs")


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
        CREATE TABLE IF NOT EXISTS auditcore.p2_drive_connections (
            tenant_id              varchar(128) NOT NULL PRIMARY KEY,
            google_email           varchar(320),
            refresh_token_sealed   text         NOT NULL,
            root_folder_id         varchar(128),
            archive_folder_id      varchar(128),
            retention_days         integer      NOT NULL DEFAULT 30 CHECK (retention_days BETWEEN 0 AND 3650),
            changes_token          varchar(200),
            connection_status      varchar(16)  NOT NULL DEFAULT 'CONNECTED'
                                     CHECK (connection_status IN ('CONNECTED', 'DISCONNECTED', 'ERROR')),
            last_error             text,
            last_synced_at_utc     timestamptz,
            connected_by_actor_id  varchar(200),
            connected_at_utc       timestamptz  NOT NULL DEFAULT now(),
            updated_at_utc         timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_drive_folders (
            tenant_id           varchar(128) NOT NULL,
            drive_folder_id     varchar(128) NOT NULL,
            folder_kind         varchar(12)  NOT NULL
                                  CHECK (folder_kind IN ('PROJECT', 'DEALER', 'OUTLET', 'PC', 'CUSTOMER', 'SUB',
                                                         'ARCHIVE')),
            parent_folder_id    varchar(128),
            folder_name         varchar(500) NOT NULL,
            dealer_id           uuid,
            outlet_id           uuid,
            folder_date         date,
            pc_actor_id         varchar(200),
            customer_folder_id  varchar(128),
            photo_lane          boolean      NOT NULL DEFAULT false,
            customer_name       varchar(300),
            booking_number      varchar(60),
            number_key          varchar(80),
            journey_id          uuid,
            made_by_system      boolean      NOT NULL DEFAULT false,
            archived_at_utc     timestamptz,
            gone_at_utc         timestamptz,
            created_at_utc      timestamptz  NOT NULL DEFAULT now(),
            updated_at_utc      timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, drive_folder_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_drive_folders_place ON auditcore.p2_drive_folders "
        "(tenant_id, folder_kind, outlet_id, folder_date)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_drive_folders_parent ON auditcore.p2_drive_folders "
        "(tenant_id, parent_folder_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_drive_folders_number ON auditcore.p2_drive_folders "
        "(tenant_id, outlet_id, number_key) WHERE folder_kind = 'CUSTOMER' AND number_key IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_drive_runs (
            tenant_id              varchar(128) NOT NULL,
            run_id                 uuid         NOT NULL DEFAULT gen_random_uuid(),
            run_trigger            varchar(12)  NOT NULL
                                     CHECK (run_trigger IN ('PC_SUBMIT', 'END_OF_DAY', 'ON_DEMAND')),
            run_mode               varchar(8)   NOT NULL DEFAULT 'NOW' CHECK (run_mode IN ('NOW', 'BATCH')),
            requested_by_actor_id  varchar(200) NOT NULL,
            requested_by_role      varchar(40),
            requested_by_name      varchar(300),
            dealer_id              uuid,
            outlet_id              uuid,
            upload_date            date,
            run_status             varchar(12)  NOT NULL DEFAULT 'REQUESTED'
                                     CHECK (run_status IN ('REQUESTED', 'RUNNING', 'READING', 'DONE', 'FAILED')),
            summary                jsonb        NOT NULL DEFAULT '{}'::jsonb,
            detail                 text,
            requested_at_utc       timestamptz  NOT NULL DEFAULT now(),
            started_at_utc         timestamptz,
            finished_at_utc        timestamptz,
            updated_at_utc         timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, run_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_drive_runs_recent ON auditcore.p2_drive_runs "
        "(tenant_id, requested_at_utc DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_drive_files (
            tenant_id              varchar(128)  NOT NULL,
            file_row_id            uuid          NOT NULL DEFAULT gen_random_uuid(),
            drive_file_id          varchar(128)  NOT NULL,
            content_md5            varchar(64)   NOT NULL DEFAULT '',
            file_name              varchar(500)  NOT NULL,
            mime_type              varchar(150)  NOT NULL,
            size_bytes             bigint        NOT NULL DEFAULT 0,
            drive_created_at_utc   timestamptz,
            drive_modified_at_utc  timestamptz,
            parent_folder_id       varchar(128),
            customer_folder_id     varchar(128),
            dealer_id              uuid,
            outlet_id              uuid,
            upload_date            date,
            pc_actor_id            varchar(200),
            lane                   varchar(10)   NOT NULL DEFAULT 'DOCUMENT' CHECK (lane IN ('DOCUMENT', 'PHOTO')),
            file_status            varchar(12)   NOT NULL DEFAULT 'NEW'
                                     CHECK (file_status IN ('NEW', 'TAKEN', 'WAITING', 'SET_ASIDE', 'FAILED')),
            detail                 text,
            object_key             varchar(1024),
            sha256                 varchar(64),
            run_id                 uuid,
            bulk_id                uuid,
            bulk_file_id           uuid,
            photo_id               uuid,
            journey_id             uuid,
            seen_at_utc            timestamptz   NOT NULL DEFAULT now(),
            taken_at_utc           timestamptz,
            updated_at_utc         timestamptz   NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, file_row_id),
            UNIQUE (tenant_id, drive_file_id, content_md5)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_drive_files_waiting ON auditcore.p2_drive_files "
        "(tenant_id, file_status, outlet_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_p2_drive_files_run ON auditcore.p2_drive_files (tenant_id, run_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_drive_files_customer ON auditcore.p2_drive_files "
        "(tenant_id, customer_folder_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_drive_files_bulk_file ON auditcore.p2_drive_files "
        "(tenant_id, bulk_file_id) WHERE bulk_file_id IS NOT NULL"
    )
    for table in _TABLES:
        _secure(table)

    # A drop that came from Drive, and what its folder already says about a file.
    op.execute("ALTER TABLE auditcore.p2_bulk_uploads ADD COLUMN IF NOT EXISTS source varchar(8) NOT NULL DEFAULT 'WEB'")
    op.execute("ALTER TABLE auditcore.p2_bulk_uploads ADD COLUMN IF NOT EXISTS drive_run_id uuid")
    op.execute("ALTER TABLE auditcore.p2_bulk_files ADD COLUMN IF NOT EXISTS target_journey_id uuid")
    op.execute("ALTER TABLE auditcore.p2_bulk_files ADD COLUMN IF NOT EXISTS booking_number varchar(60)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_bulk_uploads_drive_run ON auditcore.p2_bulk_uploads "
        "(tenant_id, drive_run_id) WHERE drive_run_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_p2_bulk_uploads_drive_run")
    op.execute("ALTER TABLE auditcore.p2_bulk_files DROP COLUMN IF EXISTS booking_number")
    op.execute("ALTER TABLE auditcore.p2_bulk_files DROP COLUMN IF EXISTS target_journey_id")
    op.execute("ALTER TABLE auditcore.p2_bulk_uploads DROP COLUMN IF EXISTS drive_run_id")
    op.execute("ALTER TABLE auditcore.p2_bulk_uploads DROP COLUMN IF EXISTS source")
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS auditcore.{table}")
