"""0183_file_moves -- another customer's file goes to that customer's own booking (2026-11-01).

File identity (0182) takes another customer's file out of a booking. This
migration adds where each such file goes, so that the customer's documents are
on a booking of their own:

- auditcore.p2_file_moves: one row per file that was taken out (its source
  batch), holding the plan and what was done. An operator inserts the rows,
  after review; the FILE_MOVE worker job (uc03_file_move) carries them out.
  The action is one of:
  - NEW_BOOKING: no booking of the customer's exists yet. A booking is
    started under the same outlet, for the PC who uploaded the file, and the
    original file is uploaded to it again; it is read there like any upload.
  - OWN_BOOKING: the customer already has their own booking holding these
    documents. Nothing is copied.
  - SAME_FILE: the same file (byte for byte) was in two bookings. It follows
    the other row's move (same_as_move_id).
  status is PLANNED, then DONE or FAILED; detail says why. The source file
  is never changed or deleted by a move.
- The web's notice on the source booking ("... taken out of this booking and
  moved to booking X") reads the DONE row (uc03_p2_file_identity.files_view).

Row security as p2_journey_file_identity: a Project reads and writes its own
rows; the runtime never deletes them (a journey delete cascades).
"""
from __future__ import annotations

from alembic import op

revision = "0183_file_moves"
down_revision = "0182_file_identity"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_file_moves"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id          varchar(128) NOT NULL,
            move_id            uuid         NOT NULL DEFAULT gen_random_uuid(),
            source_journey_id  uuid         NOT NULL,
            source_batch_id    uuid         NOT NULL,
            action             varchar(16)  NOT NULL CHECK (action IN ('NEW_BOOKING', 'OWN_BOOKING', 'SAME_FILE')),
            target_journey_id  uuid,
            same_as_move_id    uuid,
            person_name        varchar(300),
            status             varchar(12)  NOT NULL DEFAULT 'PLANNED'
                                 CHECK (status IN ('PLANNED', 'DONE', 'FAILED')),
            new_batch_id       uuid,
            detail             text,
            planned_by         varchar(200) NOT NULL,
            planned_at_utc     timestamptz  NOT NULL DEFAULT now(),
            done_at_utc        timestamptz,
            PRIMARY KEY (tenant_id, move_id),
            UNIQUE (tenant_id, source_batch_id),
            CHECK (action <> 'OWN_BOOKING' OR target_journey_id IS NOT NULL),
            CHECK (action <> 'SAME_FILE' OR same_as_move_id IS NOT NULL),
            FOREIGN KEY (tenant_id, source_journey_id) REFERENCES auditcore.journeys (tenant_id, journey_id)
              ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, source_batch_id) REFERENCES auditcore.p2_upload_batches (tenant_id, batch_id)
              ON DELETE CASCADE
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_target ON auditcore.{_TABLE} (tenant_id, target_journey_id) "
        "WHERE target_journey_id IS NOT NULL"
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


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
