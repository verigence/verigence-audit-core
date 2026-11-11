"""0210_geography_masters -- the PIN code and district masters behind the customer map (2026-11-11).

Analytics showed where each dealership's customers live on a map of Odisha, by PIN code and by
district. Its boundaries and its PIN code directory sat in the ``analytics`` schema, which belongs
to the nightly-copy service and which Audit Core's runtime role cannot read; when Analytics moved
onto Audit Core's live figures the map was lost with them.

Two reference tables, the same shape Analytics kept:

* ``geo_pin_master`` -- a PIN code, its district and its localities (postal directory);
* ``geo_boundary_master`` -- the PIN code and district outlines, GeoJSON, gzip.

They carry no tenant: a PIN code's district is the same for every Project. Where the Analytics
copies exist and can be read, their rows are brought across; elsewhere (a test database) the tables
stay empty and the map shows nothing.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0210_geography_masters"
down_revision = "0209_reread_corrections"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.geo_pin_master (
            pincode        varchar(6)  PRIMARY KEY,
            state_name     text        NOT NULL,
            district_name  text,
            locality_names jsonb       NOT NULL DEFAULT '[]'::jsonb,
            source_name    text        NOT NULL,
            source_url     text        NOT NULL,
            refreshed_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.geo_boundary_master (
            boundary_key  text        PRIMARY KEY,
            state_name    text        NOT NULL,
            boundary_type text        NOT NULL CHECK (boundary_type IN ('PIN', 'DISTRICT')),
            content_gzip  bytea       NOT NULL,
            source_name   text        NOT NULL,
            source_url    text        NOT NULL,
            refreshed_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('analytics.geo_pin_master') IS NOT NULL
               AND has_table_privilege('analytics.geo_pin_master', 'SELECT') THEN
                INSERT INTO auditcore.geo_pin_master
                    (pincode, state_name, district_name, locality_names, source_name, source_url, refreshed_at)
                SELECT pincode, state_name, district_name, locality_names, source_name, source_url, refreshed_at
                FROM analytics.geo_pin_master
                ON CONFLICT (pincode) DO NOTHING;
            END IF;
            IF to_regclass('analytics.geo_boundary_master') IS NOT NULL
               AND has_table_privilege('analytics.geo_boundary_master', 'SELECT') THEN
                INSERT INTO auditcore.geo_boundary_master
                    (boundary_key, state_name, boundary_type, content_gzip, source_name, source_url, refreshed_at)
                SELECT boundary_key, state_name, boundary_type, content_gzip, source_name, source_url, refreshed_at
                FROM analytics.geo_boundary_master
                ON CONFLICT (boundary_key) DO NOTHING;
            END IF;
        END $$;
        """
    )
    op.execute(f"GRANT SELECT ON auditcore.geo_pin_master TO {_RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT ON auditcore.geo_boundary_master TO {_RUNTIME_ROLE}")
    op.execute("COMMENT ON TABLE auditcore.geo_pin_master IS "
               "'A PIN code, its district and localities (postal directory). Reference data, no tenant.'")
    op.execute("COMMENT ON TABLE auditcore.geo_boundary_master IS "
               "'PIN code and district outlines for the customer map: GeoJSON, gzip. Reference data, no tenant.'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.geo_boundary_master")
    op.execute("DROP TABLE IF EXISTS auditcore.geo_pin_master")
