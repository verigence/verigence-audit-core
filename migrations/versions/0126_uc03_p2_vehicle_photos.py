"""0126_uc03_p2_vehicle_photos — Phase 2 vehicle photos.

Vehicle photos stay plain evidence (never sent to DI, never classified or
extracted) in auditcore.delivery_vehicle_photos. Phase 2 uploads them
straight to object storage and then records them; client_upload_id makes
that record idempotent so a retried finalize never creates a second photo,
and view_code carries the optional angle the person chose (front, rear...).
"""
from __future__ import annotations

from alembic import op

revision = "0126_uc03_p2_vehicle_photos"
down_revision = "0125_uc03_p2_group_index_merged"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.delivery_vehicle_photos
          ADD COLUMN IF NOT EXISTS client_upload_id varchar(128),
          ADD COLUMN IF NOT EXISTS view_code varchar(32),
          ADD COLUMN IF NOT EXISTS capture_source varchar(16) NOT NULL DEFAULT 'LEGACY';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_delivery_vehicle_photos_client_upload
          ON auditcore.delivery_vehicle_photos (tenant_id, journey_id, client_upload_id)
          WHERE client_upload_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS auditcore.ux_delivery_vehicle_photos_client_upload;
        ALTER TABLE auditcore.delivery_vehicle_photos
          DROP COLUMN IF EXISTS capture_source,
          DROP COLUMN IF EXISTS view_code,
          DROP COLUMN IF EXISTS client_upload_id;
        """
    )
