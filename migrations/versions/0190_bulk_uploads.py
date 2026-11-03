"""0190_bulk_uploads -- a PC's bulk upload of journey files (product owner 2026-11-03).

The PC drops many files at once for one outlet; they are stored in R2 under
dealership / outlet / day of the month, each under the name the PC gave it,
and nothing is processed until the PC submits. Each file then becomes a new
booking, an update to an existing booking, or is set aside as an exact
duplicate -- with no question to the PC (uc03_p2_bulk_upload).

- p2_bulk_uploads  one drop: the outlet, the PC, the R2 folder, its status
                   (OPEN while files are added, SUBMITTED, PROCESSING, DONE).
- p2_bulk_files    each file: its R2 key, fingerprint (SHA-256), the group of
                   files read together (one customer), and where it went:
                   EXACT_DUPLICATE, READING (read on its own journey first),
                   NEW_BOOKING, ADDED_TO_BOOKING, KEPT_FOR_REVIEW or FAILED.
                   No foreign key to journeys: a journey a file was read on
                   may be merged into the customer's booking and deleted.

Tenant row security as every Audit Core table; the runtime role may read,
add and update, never delete.
"""
from __future__ import annotations

from alembic import op

revision = "0190_bulk_uploads"
down_revision = "0189_page_fingerprint_index"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


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
        CREATE TABLE IF NOT EXISTS auditcore.p2_bulk_uploads (
            tenant_id             varchar(128)  NOT NULL,
            bulk_id               uuid          NOT NULL DEFAULT gen_random_uuid(),
            dealer_id             uuid          NOT NULL,
            outlet_id             uuid          NOT NULL,
            uploaded_by_actor_id  varchar(200)  NOT NULL,
            uploaded_by_name      varchar(300),
            folder                varchar(1024) NOT NULL,
            status                varchar(12)   NOT NULL DEFAULT 'OPEN'
                                    CHECK (status IN ('OPEN', 'SUBMITTED', 'PROCESSING', 'DONE', 'FAILED')),
            file_count            integer       NOT NULL DEFAULT 0,
            summary               jsonb         NOT NULL DEFAULT '{}'::jsonb,
            created_at_utc        timestamptz   NOT NULL DEFAULT now(),
            submitted_at_utc      timestamptz,
            finished_at_utc       timestamptz,
            updated_at_utc        timestamptz   NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, bulk_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_bulk_uploads_owner ON auditcore.p2_bulk_uploads "
        "(tenant_id, uploaded_by_actor_id, created_at_utc DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_bulk_files (
            tenant_id          varchar(128)  NOT NULL,
            file_id            uuid          NOT NULL DEFAULT gen_random_uuid(),
            bulk_id            uuid          NOT NULL,
            original_filename  varchar(500)  NOT NULL,
            content_type       varchar(100)  NOT NULL,
            size_bytes         bigint        NOT NULL DEFAULT 0,
            object_key         varchar(1024) NOT NULL,
            upload_status      varchar(16)   NOT NULL DEFAULT 'AWAITING_UPLOAD'
                                 CHECK (upload_status IN ('AWAITING_UPLOAD', 'UPLOADED')),
            sha256             varchar(64),
            group_key          varchar(300),
            outcome            varchar(20)   NOT NULL DEFAULT 'PENDING'
                                 CHECK (outcome IN ('PENDING', 'EXACT_DUPLICATE', 'READING', 'NEW_BOOKING',
                                                    'ADDED_TO_BOOKING', 'KEPT_FOR_REVIEW', 'FAILED')),
            journey_id         uuid,
            batch_id           uuid,
            placed_journey_id  uuid,
            placed_reference   varchar(100),
            detail             text,
            created_at_utc     timestamptz   NOT NULL DEFAULT now(),
            updated_at_utc     timestamptz   NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, file_id),
            UNIQUE (tenant_id, object_key),
            FOREIGN KEY (tenant_id, bulk_id) REFERENCES auditcore.p2_bulk_uploads (tenant_id, bulk_id)
              ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_p2_bulk_files_bulk ON auditcore.p2_bulk_files (tenant_id, bulk_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_bulk_files_journey ON auditcore.p2_bulk_files (tenant_id, journey_id) "
        "WHERE journey_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_bulk_files_sha ON auditcore.p2_bulk_files (tenant_id, sha256) "
        "WHERE sha256 IS NOT NULL"
    )
    _secure("p2_bulk_uploads")
    _secure("p2_bulk_files")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.p2_bulk_files")
    op.execute("DROP TABLE IF EXISTS auditcore.p2_bulk_uploads")
