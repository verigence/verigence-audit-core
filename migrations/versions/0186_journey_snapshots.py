"""0186_journey_snapshots -- what Journey 360 shows, kept per run (2026-11-02).

A structural change to how the Deal, Payments, Add-ons, Trade-in and Audit
Summary figures are worked out must be checked on every live booking, not
only on the booking that was reported (product owner, 2026-11-02: "fix the
house" first). The JOURNEY_SNAPSHOT worker job (uc03_journey_snapshot)
renders those Journey 360 sections for every booking of a Project, read-only,
exactly as the screens build them, and keeps each one here under a tag. Two
tags taken before and after a change are compared by the
JOURNEY_SNAPSHOT_DIFF job: what changed, on how many bookings, where.

- auditcore.p2_journey_snapshots: one row per (tag, booking, section) with
  the section's JSON as served, or the error that building it raised.
  Snapshots are derived data, never a source for anything else; the job
  keeps the latest few tags of a Project and deletes older ones.

Row security as every Project table: a Project reads and writes its own rows.
"""
from __future__ import annotations

from alembic import op

revision = "0186_journey_snapshots"
down_revision = "0185_entered_name_follows"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_journey_snapshots"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id          varchar(128) NOT NULL,
            tag                varchar(200) NOT NULL,
            journey_id         uuid         NOT NULL,
            journey_reference  varchar(160),
            section            varchar(40)  NOT NULL,
            payload            jsonb,
            error              text,
            taken_at_utc       timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, tag, journey_id, section),
            FOREIGN KEY (tenant_id, journey_id) REFERENCES auditcore.journeys (tenant_id, journey_id)
              ON DELETE CASCADE
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_taken ON auditcore.{_TABLE} (tenant_id, tag, taken_at_utc)")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    # Derived data: the job replaces a tag it takes again and drops old tags.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
