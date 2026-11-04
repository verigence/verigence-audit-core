"""0194_dealguard_location_checks -- a PC's location, checked at random while
they use DealGuard (product owner, 2026-11-05).

"While a PC is using DealGuard the app checks the PC's location at random
moments: between 1 and 3 times in each clock hour, only between 10:00 and
19:30 IST ... Every check is logged." The app sends where the device is; Audit
Core works out the distance to the nearest outlet the PC is assigned to and
classifies it (uc03_p2_location_checks). A PC found away from their outlets is
only told, nothing is blocked.

``p2_location_checks`` holds one row per check: who, when, the result, the
distance and the nearest outlet, how accurate the device said it was, and where
the reading came from. A check is never changed or removed once written
(append-only: the runtime role may only read and insert). Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0194_dealguard_location_checks"
down_revision = "0193_same_dealership_alert"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_location_checks"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            check_id            uuid          NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id           varchar(128)  NOT NULL,
            actor_id            varchar(160)  NOT NULL,
            checked_at_utc      timestamptz   NOT NULL DEFAULT now(),
            -- When the device says it read the location; the server's own time is checked_at_utc.
            captured_at_utc     timestamptz,
            check_status        varchar(30)   NOT NULL
                                CHECK (check_status IN ('AT_OUTLET', 'AWAY', 'UNCLEAR', 'NO_OUTLET_LOCATION',
                                                        'DENIED', 'UNAVAILABLE')),
            latitude            numeric(9,6),
            longitude           numeric(9,6),
            accuracy_m          numeric(10,1),
            -- Worked out by Audit Core (Haversine), never taken from the device.
            distance_m          numeric(12,1),
            nearest_outlet_id   uuid,
            nearest_outlet_name varchar(500),
            location_source     varchar(10)   NOT NULL CHECK (location_source IN ('NATIVE', 'BROWSER')),
            device_type         varchar(20)   CHECK (device_type IN ('WEB', 'MOBILE_WEB', 'ANDROID', 'IOS')),
            -- Why the device gave no location (permission refused, timed out), as it said.
            client_detail       varchar(300)
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_day ON auditcore.{_TABLE} (tenant_id, checked_at_utc DESC)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_actor ON auditcore.{_TABLE} "
        "(tenant_id, actor_id, checked_at_utc DESC)"
    )
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    # Append-only: a check is read and written, never changed or removed.
    op.execute(f"GRANT SELECT, INSERT ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON auditcore.{_TABLE} FROM {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
