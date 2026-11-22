"""0256_dms_not_in_dms -- the bookings in Verigence that the DMS does not have, per Journey (2026-11-22).

Product owner, 22 Nov 2026: "can we highlight these which are not in dms but in
verigence, to identify the same can we add a new filter or simply a switch not in dms".

- p2_journey_not_in_dms  one row per Journey the latest DMS report of its dealership lists
                         as "In Verigence, not in DMS", with the cross-check's reason
                         (uc03_p2_dms_booking_crosscheck). Written when a report is generated
                         or rebuilt; a report older than the dealership's newest writes
                         nothing. A Journey the next report no longer lists is switched off
                         (active = false), not deleted.

The Bookings & Deliveries list's "Not in DMS" switch reads it.

Tenant row security as every Audit Core table; the runtime role may read, add and update,
never delete.
"""
from __future__ import annotations

from alembic import op

revision = "0256_dms_not_in_dms"
down_revision = "0255_pages_of_a_file_index"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_journey_not_in_dms"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id       varchar(128) NOT NULL,
            journey_id      uuid NOT NULL,
            dealer_id       uuid NOT NULL,
            report_id       uuid NOT NULL,
            report_date     date NOT NULL,
            dump_date       date,
            booking_date    date,
            reason_code     varchar(40) NOT NULL,
            reason          text NOT NULL,
            detail          text,
            active          boolean NOT NULL DEFAULT true,
            updated_at_utc  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id)
        )
    """)
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_dealer ON auditcore.{_TABLE} (tenant_id, dealer_id) "
               "WHERE active")
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
