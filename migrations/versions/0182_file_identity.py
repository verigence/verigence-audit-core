"""0182_file_identity -- whose each uploaded file is; another customer's file leaves the booking (2026-10-31).

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
- auditcore.p2_file_identity_enforcement: where enforcement is on, switched
  from the database without a redeploy -- one row per journey (a pilot) or a
  Project-wide row (journey_id NULL), unique on (tenant_id, journey_id or
  '*'). Enforcement is on for a journey when P2_FILE_IDENTITY_ENFORCE is set
  (a global override) or an enabled row covers it; a row turned to enabled =
  false puts the journey back as it was on its next recompute, which the
  switch itself asks for (trigger). Row security
  as p2_upload_pause: a Project reads its own rows, only a SuperAdmin request
  (app.platform_super_admin) writes; never deleted by the runtime.
- auditcore.evidence.foreign_restore_state: on a copy file identity made
  active again, what it was before (void reason, who, when, capture status),
  so switching enforcement off supersedes it again exactly as it was.
"""
from __future__ import annotations

from alembic import op

revision = "0182_file_identity"
down_revision = "0181_upload_pause"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_journey_file_identity"
_SWITCH = "p2_file_identity_enforcement"


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
          ADD COLUMN IF NOT EXISTS pre_foreign_status varchar(30),
          ADD COLUMN IF NOT EXISTS foreign_restore_state jsonb;
        """
    )
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_SWITCH} (
            enforcement_id   uuid         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id        varchar(128) NOT NULL,
            journey_id       uuid,
            enabled          boolean      NOT NULL,
            set_by_actor_id  varchar(160),
            set_at_utc       timestamptz  NOT NULL DEFAULT now(),
            note             text,
            FOREIGN KEY (tenant_id, journey_id) REFERENCES auditcore.journeys (tenant_id, journey_id)
              ON DELETE CASCADE
        )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{_SWITCH}_scope "
        f"ON auditcore.{_SWITCH} (tenant_id, (COALESCE(journey_id::text, '*')))"
    )
    op.execute(f"ALTER TABLE auditcore.{_SWITCH} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_SWITCH} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {_SWITCH}_read ON auditcore.{_SWITCH}")
    op.execute(
        f"CREATE POLICY {_SWITCH}_read ON auditcore.{_SWITCH} FOR SELECT "
        "USING (tenant_id = auditcore.current_tenant_id() OR auditcore.current_platform_super_admin())"
    )
    op.execute(f"DROP POLICY IF EXISTS {_SWITCH}_admin_insert ON auditcore.{_SWITCH}")
    op.execute(
        f"CREATE POLICY {_SWITCH}_admin_insert ON auditcore.{_SWITCH} FOR INSERT "
        "WITH CHECK (auditcore.current_platform_super_admin())"
    )
    op.execute(f"DROP POLICY IF EXISTS {_SWITCH}_admin_update ON auditcore.{_SWITCH}")
    op.execute(
        f"CREATE POLICY {_SWITCH}_admin_update ON auditcore.{_SWITCH} FOR UPDATE "
        "USING (auditcore.current_platform_super_admin()) "
        "WITH CHECK (auditcore.current_platform_super_admin())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_SWITCH} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON auditcore.{_SWITCH} FROM {_RUNTIME_ROLE}")
    # A switch turned on or off takes effect on the worker's next pass: the
    # booking (or, for a Project-wide row, every booking holding a foreign
    # file or one taken out) is asked for a recompute. Under the row's own
    # tenant, restored afterwards: the SuperAdmin session has none.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION auditcore.{_SWITCH}_changed()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $fn$
        DECLARE
            previous_tenant text := current_setting('app.tenant_id', true);
            booking uuid;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.enabled IS NOT DISTINCT FROM OLD.enabled THEN
                RETURN NEW;
            END IF;
            PERFORM set_config('app.tenant_id', NEW.tenant_id, true);
            FOR booking IN
                SELECT b.journey_id FROM auditcore.p2_upload_batches b
                WHERE b.tenant_id = NEW.tenant_id
                  AND (b.journey_id = NEW.journey_id
                       OR (NEW.journey_id IS NULL
                           AND (b.identity_status = 'FOREIGN' OR b.identity_enforced_at_utc IS NOT NULL)))
                GROUP BY b.journey_id
            LOOP
                PERFORM auditcore.p2_request_stage_recompute(NEW.tenant_id, booking);
            END LOOP;
            PERFORM set_config('app.tenant_id', COALESCE(previous_tenant, ''), true);
            RETURN NEW;
        END
        $fn$;
        DROP TRIGGER IF EXISTS trg_{_SWITCH}_changed ON auditcore.{_SWITCH};
        CREATE TRIGGER trg_{_SWITCH}_changed
          AFTER INSERT OR UPDATE OF enabled ON auditcore.{_SWITCH}
          FOR EACH ROW EXECUTE FUNCTION auditcore.{_SWITCH}_changed();
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
        DROP TABLE IF EXISTS auditcore.{_SWITCH};
        DROP FUNCTION IF EXISTS auditcore.{_SWITCH}_changed();
        UPDATE auditcore.evidence
           SET association_status = 'SUPERSEDED',
               void_reason = foreign_restore_state->>'voidReason',
               voided_by_actor_id = foreign_restore_state->>'voidedBy',
               voided_at_utc = (foreign_restore_state->>'voidedAt')::timestamptz
         WHERE foreign_restore_state IS NOT NULL AND association_status = 'ACTIVE';
        UPDATE auditcore.evidence
           SET association_status = COALESCE(pre_foreign_status, 'SUPERSEDED')
         WHERE association_status = 'FOREIGN';
        ALTER TABLE auditcore.evidence
          DROP CONSTRAINT IF EXISTS evidence_association_status_check,
          ADD CONSTRAINT evidence_association_status_check
            CHECK (association_status IN ('ACTIVE', 'VOIDED', 'SUPERSEDED', 'UNLINKED')),
          DROP COLUMN IF EXISTS pre_foreign_status,
          DROP COLUMN IF EXISTS foreign_restore_state;
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
