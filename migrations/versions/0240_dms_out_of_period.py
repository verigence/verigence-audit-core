"""A DMS dump row dated outside the days its dump is compared for (product owner,
2026-11-18: a dump uploaded on the 18th is compared for the 1st to the 17th) is kept
as OUT_OF_PERIOD -- in the file, not counted, not reported.
"""
from __future__ import annotations

from alembic import op

revision = "0240_dms_out_of_period"
down_revision = "0239_observation_severity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$
        DECLARE c text;
        BEGIN
          SELECT conname INTO c FROM pg_constraint
          WHERE conrelid = 'auditcore.p2_dms_dump_rows'::regclass AND contype = 'c'
            AND pg_get_constraintdef(oid) LIKE '%NOT_IN_TOOL%';
          IF c IS NOT NULL THEN
            EXECUTE format('ALTER TABLE auditcore.p2_dms_dump_rows DROP CONSTRAINT %I', c);
          END IF;
        END $$;
    """)
    op.execute("""
        ALTER TABLE auditcore.p2_dms_dump_rows ADD CONSTRAINT p2_dms_dump_rows_outcome_check
        CHECK (outcome IN ('MATCHED', 'NOT_IN_TOOL', 'MISSING', 'CANCELLED', 'OUT_OF_PERIOD'))
    """)


def downgrade() -> None:
    op.execute("UPDATE auditcore.p2_dms_dump_rows SET outcome = 'MISSING' WHERE outcome = 'OUT_OF_PERIOD'")
    op.execute("ALTER TABLE auditcore.p2_dms_dump_rows DROP CONSTRAINT p2_dms_dump_rows_outcome_check")
    op.execute("""
        ALTER TABLE auditcore.p2_dms_dump_rows ADD CONSTRAINT p2_dms_dump_rows_outcome_check
        CHECK (outcome IN ('MATCHED', 'NOT_IN_TOOL', 'MISSING', 'CANCELLED'))
    """)
