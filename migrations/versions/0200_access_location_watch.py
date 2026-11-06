"""0200_access_location_watch -- where each PC, TL and PM opens Verigence from
(product owner, 2026-11-06).

"I would like to keep a watch on the locations from which an id is accessed. One
location is the location to which the PC is tagged, geofenced: logging in within
500 metres is that location. Keep count of the times PC/TL/PM log in from a given
location till 22 Nov 2026, and turn the top 3 into a readable address. Only for
SuperAdmin and CEO/Executive."

``p2_access_locations`` holds one row per sign-in or resume of the app in the
Project: who, when, their role, where the device said it was, and the nearest
outlet they are assigned to with the distance (worked out here, never taken from
the device). Append-only, like ``p2_location_checks``.

``p2_place_addresses`` keeps the readable address Google gave for a place
(coordinates rounded to about 100 m), so each place is looked up once.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0200_access_location_watch"
down_revision = "0199_discount_reevaluation"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def _tenant_policy(table: str) -> None:
    op.execute(f"ALTER TABLE auditcore.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON auditcore.{table}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{table} ON auditcore.{table} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_access_locations (
            access_id           uuid          NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
            tenant_id           varchar(128)  NOT NULL,
            actor_id            varchar(160)  NOT NULL,
            role_code           varchar(30)   NOT NULL,
            accessed_at_utc     timestamptz   NOT NULL DEFAULT now(),
            captured_at_utc     timestamptz,
            access_kind         varchar(20)   NOT NULL CHECK (access_kind IN ('SIGN_IN', 'RESUME')),
            outcome             varchar(20)   NOT NULL CHECK (outcome IN ('LOCATED', 'DENIED', 'UNAVAILABLE')),
            latitude            numeric(9,6),
            longitude           numeric(9,6),
            accuracy_m          numeric(10,1),
            nearest_outlet_id   uuid,
            nearest_outlet_name varchar(500),
            distance_m          numeric(12,1),
            at_tagged_outlet    boolean       NOT NULL DEFAULT false,
            location_source     varchar(10)   CHECK (location_source IN ('NATIVE', 'BROWSER')),
            device_type         varchar(20)   CHECK (device_type IN ('WEB', 'MOBILE_WEB', 'ANDROID', 'IOS'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_access_locations_actor "
        "ON auditcore.p2_access_locations (tenant_id, actor_id, accessed_at_utc DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_access_locations_day "
        "ON auditcore.p2_access_locations (tenant_id, accessed_at_utc DESC)"
    )
    _tenant_policy("p2_access_locations")
    op.execute(f"GRANT SELECT, INSERT ON auditcore.p2_access_locations TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON auditcore.p2_access_locations FROM {_RUNTIME_ROLE}")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_place_addresses (
            tenant_id       varchar(128) NOT NULL,
            lat_key         numeric(7,3) NOT NULL,
            lng_key         numeric(7,3) NOT NULL,
            address         varchar(500) NOT NULL,
            looked_up_at    timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, lat_key, lng_key)
        )
        """
    )
    _tenant_policy("p2_place_addresses")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.p2_place_addresses TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.p2_place_addresses")
    op.execute("DROP TABLE IF EXISTS auditcore.p2_access_locations")
