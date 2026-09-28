"""0128_uc03_p2_journey_id_timing — internal Journey ID and stage timing.

1. Internal Journey ID. journeys.journey_reference was never set by any
   creation path, so the only identifier was the UUID. Every new Journey
   now gets a readable, unique ID on insert (VJ<yymm>-<6-digit sequence>,
   e.g. VJ2609-000123) from a database trigger, so every creation path --
   Phase 1 and Phase 2 -- gets one; existing Journeys are backfilled in
   creation order. A reference already set is never overwritten.

Stage timing is not duplicated here: Phase 2 records milestones in the
existing journey workflow (journey_stage_states + journey_workflow_events).
"""
from __future__ import annotations

from alembic import op

revision = "0128_uc03_p2_journey_id_timing"
down_revision = "0127_uc03_p2_list_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SEQUENCE IF NOT EXISTS auditcore.journey_number_seq START 1;
        GRANT USAGE, SELECT ON SEQUENCE auditcore.journey_number_seq TO audit_core_runtime;
        -- Never re-issue an ID already on a Journey (e.g. after a downgrade).
        SELECT setval('auditcore.journey_number_seq',
                      COALESCE((SELECT MAX(split_part(journey_reference, '-', 2)::bigint)
                                FROM auditcore.journeys
                                WHERE journey_reference ~ '^VJ[0-9]{4}-[0-9]+$'), 0) + 1, false);

        CREATE OR REPLACE FUNCTION auditcore.assign_journey_reference() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.journey_reference IS NULL OR btrim(NEW.journey_reference) = '' THEN
            NEW.journey_reference := 'VJ'
              || to_char(COALESCE(NEW.created_at_utc, now()) AT TIME ZONE 'Asia/Kolkata', 'YYMM')
              || '-' || lpad(nextval('auditcore.journey_number_seq')::text, 6, '0');
          END IF;
          RETURN NEW;
        END;
        $$;

        DROP TRIGGER IF EXISTS trg_journeys_assign_reference ON auditcore.journeys;
        CREATE TRIGGER trg_journeys_assign_reference
          BEFORE INSERT ON auditcore.journeys
          FOR EACH ROW EXECUTE FUNCTION auditcore.assign_journey_reference();

        -- Backfill without touching updated_at (lists sort by activity).
        ALTER TABLE auditcore.journeys DISABLE TRIGGER trg_journeys_updated_at;
        WITH numbered AS (
          SELECT tenant_id, journey_id, created_at_utc
          FROM auditcore.journeys
          WHERE journey_reference IS NULL OR btrim(journey_reference) = ''
          ORDER BY created_at_utc, journey_id
        )
        UPDATE auditcore.journeys j
           SET journey_reference = 'VJ'
             || to_char(n.created_at_utc AT TIME ZONE 'Asia/Kolkata', 'YYMM')
             || '-' || lpad(nextval('auditcore.journey_number_seq')::text, 6, '0')
          FROM numbered n
         WHERE j.tenant_id = n.tenant_id AND j.journey_id = n.journey_id;
        ALTER TABLE auditcore.journeys ENABLE TRIGGER trg_journeys_updated_at;

        CREATE UNIQUE INDEX IF NOT EXISTS ux_journeys_internal_reference
          ON auditcore.journeys (journey_reference) WHERE journey_reference LIKE 'VJ____-%';

        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS auditcore.ux_journeys_internal_reference;
        DROP TRIGGER IF EXISTS trg_journeys_assign_reference ON auditcore.journeys;
        DROP FUNCTION IF EXISTS auditcore.assign_journey_reference();
        DROP SEQUENCE IF EXISTS auditcore.journey_number_seq;
        """
    )
