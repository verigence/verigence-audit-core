"""0209_reread_corrections -- a person's corrections survive a page being read again (2026-11-10).

Reading a page again (a PC's or Reviewer's "Set the document type", or a job doing the same)
retires the document it was and reads the stored page anew; every value a person had corrected on
it was lost with it. Live 2026-11-10, UTK-SU-261020-005: the Reviewer set the struck-out
accessories Rs 7,000 to 0 on the cost sheet; the page was read again as the NDC and the Rs 7,000
came back.

``p2_reread_corrections`` keeps, per page sent again, the corrections the retired document carried.
Once the new reading is ready (uc03_reread_corrections.settle, run by the worker), a correction of
a field the new reading also has is applied to it again, and the rest are listed in one task for the
journey's PC to re-check on the new reading.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0209_reread_corrections"
down_revision = "0208_journey_delete_fk_indexes"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_reread_corrections"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id          varchar(128) NOT NULL,
            to_queue_id        uuid         NOT NULL,
            from_queue_id      uuid         NOT NULL,
            journey_id         uuid         NOT NULL,
            from_document_id   uuid         NOT NULL,
            from_document_type varchar(120),
            corrections        jsonb        NOT NULL,
            recorded_at_utc    timestamptz  NOT NULL DEFAULT now(),
            settled_at_utc     timestamptz,
            outcome            jsonb,
            PRIMARY KEY (tenant_id, to_queue_id),
            FOREIGN KEY (tenant_id, journey_id) REFERENCES auditcore.journeys (tenant_id, journey_id)
                ON DELETE CASCADE
        )
        """
    )
    # Indexed for the journey delete's foreign-key check (0208's rule).
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_fk_{_TABLE}_journey_id ON auditcore.{_TABLE} (tenant_id, journey_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_open ON auditcore.{_TABLE} (tenant_id) "
               "WHERE settled_at_utc IS NULL")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
