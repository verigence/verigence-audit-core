"""0181_file_identity -- whose each uploaded file is; another customer's file leaves the booking (2026-10-31).

Found live: on the web "New booking" screen the first file creates the booking
and the page becomes that booking, so the next customer's PDF went into the
same booking (ADI-RD-261022-022: "Azizur Reheman.pdf", then "Tapan kumar
patra.pdf"; ADI-RD-261022-026: 14 customers' files). Each newer single-slot
copy superseded the earlier one, so Tapan's PAN, Aadhaar and docket replaced
Azizur's and his receipts counted as Azizur's payments.

- auditcore.p2_upload_batches: the file's identity as the journey recompute
  decides it (uc03_p2_file_identity): BELONGS / FOREIGN / UNKNOWN, why, the
  person it names (name and masked PAN), when, and when its documents were
  taken out of the booking.
- auditcore.evidence: association_status FOREIGN -- a FOREIGN file's document,
  out of the booking for every reader of ACTIVE evidence -- and the status it
  had before (pre_foreign_status), so a file found to belong after all comes
  back exactly as it was.
- auditcore.p2_journey_file_identity: the booking's customer as its files say,
  how many files are foreign or to be checked, and the checks reported, never
  acted on (two individual PANs in one kept file, more than one car).
"""
from __future__ import annotations

from alembic import op

revision = "0181_file_identity"
down_revision = "0180_worker_churn"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_journey_file_identity"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_upload_batches
          ADD COLUMN IF NOT EXISTS identity_status varchar(16)
            CHECK (identity_status IN ('BELONGS', 'FOREIGN', 'UNKNOWN')),
          ADD COLUMN IF NOT EXISTS identity_reason_code varchar(40),
          ADD COLUMN IF NOT EXISTS identity_reason text,
          ADD COLUMN IF NOT EXISTS identity_person_name varchar(300),
          ADD COLUMN IF NOT EXISTS identity_masked_pan varchar(16),
          ADD COLUMN IF NOT EXISTS identity_decided_at_utc timestamptz,
          ADD COLUMN IF NOT EXISTS identity_enforced_at_utc timestamptz;
        CREATE INDEX IF NOT EXISTS ix_p2_upload_batches_foreign
          ON auditcore.p2_upload_batches (tenant_id, journey_id) WHERE identity_status = 'FOREIGN';

        ALTER TABLE auditcore.evidence
          DROP CONSTRAINT IF EXISTS evidence_association_status_check,
          ADD CONSTRAINT evidence_association_status_check
            CHECK (association_status IN ('ACTIVE', 'VOIDED', 'SUPERSEDED', 'UNLINKED', 'FOREIGN')),
          ADD COLUMN IF NOT EXISTS pre_foreign_status varchar(30);
        """
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id            varchar(128) NOT NULL,
            journey_id           uuid         NOT NULL,
            customer_name        varchar(300),
            customer_masked_pan  varchar(16),
            customer_kind        varchar(10)  CHECK (customer_kind IN ('PERSON', 'ORG')),
            foreign_files        integer      NOT NULL DEFAULT 0 CHECK (foreign_files >= 0),
            unknown_files        integer      NOT NULL DEFAULT 0 CHECK (unknown_files >= 0),
            checks               jsonb        NOT NULL DEFAULT '[]'::jsonb,
            decided_at_utc       timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id),
            FOREIGN KEY (tenant_id, journey_id) REFERENCES auditcore.journeys (tenant_id, journey_id)
              ON DELETE CASCADE
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_foreign ON auditcore.{_TABLE} (tenant_id) "
        "WHERE foreign_files > 0"
    )
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON auditcore.{_TABLE} FROM {_RUNTIME_ROLE}")
    # System Health: per Project, the bookings holding another customer's
    # files and the files to be checked (counts only, like pipeline_health).
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION platform_ops.file_identity_health()
        RETURNS jsonb
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $fn$
          SELECT COALESCE(jsonb_agg(jsonb_build_object(
                   'tenantId', t.tenant_id, 'foreignJourneys', t.foreign_journeys, 'foreignFiles', t.foreign_files,
                   'enforcedFiles', t.enforced_files, 'unknownJourneys', t.unknown_journeys,
                   'unknownFiles', t.unknown_files) ORDER BY t.tenant_id), '[]'::jsonb)
          FROM (
            SELECT b.tenant_id,
                   count(DISTINCT b.journey_id) FILTER (WHERE b.identity_status = 'FOREIGN') AS foreign_journeys,
                   count(*) FILTER (WHERE b.identity_status = 'FOREIGN') AS foreign_files,
                   count(*) FILTER (WHERE b.identity_status = 'FOREIGN'
                                      AND b.identity_enforced_at_utc IS NOT NULL) AS enforced_files,
                   count(DISTINCT b.journey_id) FILTER (WHERE b.identity_status = 'UNKNOWN') AS unknown_journeys,
                   count(*) FILTER (WHERE b.identity_status = 'UNKNOWN') AS unknown_files
            FROM auditcore.p2_upload_batches b
            WHERE b.identity_status IN ('FOREIGN', 'UNKNOWN')
            GROUP BY b.tenant_id
          ) t
        $fn$;
        REVOKE ALL ON FUNCTION platform_ops.file_identity_health() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION platform_ops.file_identity_health() TO {_RUNTIME_ROLE};
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DROP FUNCTION IF EXISTS platform_ops.file_identity_health();
        UPDATE auditcore.evidence
           SET association_status = COALESCE(pre_foreign_status, 'SUPERSEDED')
         WHERE association_status = 'FOREIGN';
        ALTER TABLE auditcore.evidence
          DROP CONSTRAINT IF EXISTS evidence_association_status_check,
          ADD CONSTRAINT evidence_association_status_check
            CHECK (association_status IN ('ACTIVE', 'VOIDED', 'SUPERSEDED', 'UNLINKED')),
          DROP COLUMN IF EXISTS pre_foreign_status;
        DROP TABLE IF EXISTS auditcore.{_TABLE};
        DROP INDEX IF EXISTS auditcore.ix_p2_upload_batches_foreign;
        ALTER TABLE auditcore.p2_upload_batches
          DROP COLUMN IF EXISTS identity_status,
          DROP COLUMN IF EXISTS identity_reason_code,
          DROP COLUMN IF EXISTS identity_reason,
          DROP COLUMN IF EXISTS identity_person_name,
          DROP COLUMN IF EXISTS identity_masked_pan,
          DROP COLUMN IF EXISTS identity_decided_at_utc,
          DROP COLUMN IF EXISTS identity_enforced_at_utc;
        """
    )
