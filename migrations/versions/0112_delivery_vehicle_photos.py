"""Raw vehicle-photo storage for Delivery -- deliberately outside DI.

Revision ID: 0112_delivery_vehicle_photos
Revises: 0111_backfill_warranty_alias
Create Date: 2026-09-24

Direct product ask: a PC needs to upload 5-6 photos of the delivered
vehicle as proof, but these are plain photos, not a business document --
they must never be sent through DI for classification/extraction (DI is
"Document Intelligence"; there is no intelligence to extract from a car
photo). Audit-core has never stored a raw file itself before -- every
document until now has gone through DI's own storage layer -- so this is
new, deliberately minimal storage: one row per uploaded photo, an object
key into a small S3-compatible bucket audit-core owns directly (see
audit_core.vehicle_photo_storage), and nothing else. No classification
state, no requirement-catalog entry, no extraction fields.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0112_delivery_vehicle_photos"
down_revision = "0111_backfill_warranty_alias"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            """
            CREATE TABLE auditcore.delivery_vehicle_photos (
                tenant_id               varchar(128) NOT NULL,
                photo_id                uuid NOT NULL DEFAULT gen_random_uuid(),
                journey_id              uuid NOT NULL,
                object_key              varchar(512) NOT NULL,
                original_filename       varchar(255) NOT NULL,
                content_type            varchar(120) NOT NULL,
                size_bytes              bigint NOT NULL,
                uploaded_by_actor_id    varchar(128) NOT NULL,
                uploaded_at_utc         timestamptz NOT NULL DEFAULT now(),
                deleted_by_actor_id     varchar(128),
                deleted_at_utc          timestamptz,
                PRIMARY KEY (tenant_id, photo_id),
                FOREIGN KEY (tenant_id, journey_id)
                    REFERENCES auditcore.journeys(tenant_id, journey_id)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX ix_delivery_vehicle_photos_journey
            ON auditcore.delivery_vehicle_photos (tenant_id, journey_id)
            WHERE deleted_at_utc IS NULL
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.delivery_vehicle_photos ENABLE ROW LEVEL SECURITY
            """
        )
    )
    conn.execute(
        text(
            """
            ALTER TABLE auditcore.delivery_vehicle_photos FORCE ROW LEVEL SECURITY
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE POLICY tenant_isolation_delivery_vehicle_photos
            ON auditcore.delivery_vehicle_photos
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id())
            """
        )
    )
    conn.execute(
        text(
            f"""
            GRANT SELECT, INSERT, UPDATE ON auditcore.delivery_vehicle_photos
            TO {_RUNTIME_ROLE}
            """
        )
    )
    # Soft-delete only (deleted_at_utc/deleted_by_actor_id) -- no hard DELETE,
    # matching this codebase's standing audit-trail-preservation convention.
    conn.execute(
        text(
            f"""
            REVOKE DELETE ON auditcore.delivery_vehicle_photos FROM {_RUNTIME_ROLE}
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP INDEX IF EXISTS auditcore.ix_delivery_vehicle_photos_journey"))
    conn.execute(text("DROP TABLE IF EXISTS auditcore.delivery_vehicle_photos"))
