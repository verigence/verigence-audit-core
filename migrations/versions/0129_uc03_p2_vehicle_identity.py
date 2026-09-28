"""0129_uc03_p2_vehicle_identity — VIN / chassis / engine entered by the PC.

Delivery needs proof of the vehicle: pictures of the car being delivered,
or -- when no pictures are available -- the VIN / chassis / engine number
entered by the PC on the Document Missing task. Recorded here (who, when,
which task) and reviewed by the TL as part of the Delivery review.
"""
from __future__ import annotations

from alembic import op

revision = "0129_uc03_p2_vehicle_identity"
down_revision = "0128_uc03_p2_journey_id_timing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_vehicle_identifications (
          tenant_id varchar(128) NOT NULL,
          identification_id uuid NOT NULL DEFAULT gen_random_uuid(),
          journey_id uuid NOT NULL,
          vin varchar(64),
          chassis_number varchar(64),
          engine_number varchar(64),
          entered_by_actor_id varchar(128) NOT NULL,
          entered_by_role varchar(32),
          task_id uuid,
          created_at_utc timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, identification_id),
          FOREIGN KEY (tenant_id, journey_id) REFERENCES auditcore.journeys(tenant_id, journey_id) ON DELETE CASCADE,
          CHECK (vin IS NOT NULL OR chassis_number IS NOT NULL OR engine_number IS NOT NULL)
        );
        CREATE INDEX IF NOT EXISTS ix_p2_vehicle_identifications_journey
          ON auditcore.p2_vehicle_identifications (tenant_id, journey_id, created_at_utc DESC);
        ALTER TABLE auditcore.p2_vehicle_identifications ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_vehicle_identifications FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS p2_vehicle_identifications_tenant ON auditcore.p2_vehicle_identifications;
        CREATE POLICY p2_vehicle_identifications_tenant ON auditcore.p2_vehicle_identifications
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());
        GRANT SELECT, INSERT ON auditcore.p2_vehicle_identifications TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.p2_vehicle_identifications")
