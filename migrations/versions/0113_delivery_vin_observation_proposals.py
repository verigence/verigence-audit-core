"""0113_delivery_vin_observation_proposals — TL-gated manual VIN/chassis entry.

Direct product instruction: a PC's own document-upload flow stays 100%
entry-free. The vehicle-proof requirement already has a working self-heal
path (DELIVERY_VEHICLE_PHOTOS_MISSING, 0112) that closes the instant a
photo is uploaded -- but if a PC can't get a photo, they need to enter the
VIN/chassis manually instead, and because that has no photographic proof
behind it, a Team Lead must approve it before it's treated as real. Today
`record_delivery_vehicle_observation` writes straight to
journey_delivery_audit_facts with no such gate.

Mirrors journey_document_field_correction_proposals's current shape
(0106's workflow_task_id-keyed version, not its original 0090 audit-
finding-keyed one) but as a fresh table with no historical baggage, so it
goes straight to the simpler natural key: PRIMARY KEY (tenant_id,
workflow_task_id), one proposal per DELIVERY_VIN_MANUAL_ENTRY_REVIEW task.
`computed_reconciliation_status` is computed read-only at propose time
(reusing the existing _vin_reconciliation() helper unchanged) so the TL's
task card can show MATCH/MISMATCH/REVIEW_REQUIRED before approving --
applied_at_utc stays NULL until a TL actually approves it, at which point
the same status (recomputed, not trusted stale) is written to
journey_delivery_audit_facts.
"""
from __future__ import annotations

from alembic import op

revision = "0113_delivery_vin_proposals"
down_revision = "0112_delivery_vehicle_photos"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.journey_delivery_vin_observation_proposals (
            tenant_id                     varchar(128) NOT NULL,
            workflow_task_id              uuid NOT NULL,
            journey_id                    uuid NOT NULL,
            observed_vin                  varchar(120),
            observed_chassis_number       varchar(120),
            computed_reconciliation_status varchar(30) NOT NULL
                                          CHECK (computed_reconciliation_status IN (
                                              'MATCH','MISMATCH','REVIEW_REQUIRED'
                                          )),
            proposed_by_actor_id          varchar(160) NOT NULL,
            proposed_at_utc               timestamptz NOT NULL DEFAULT now(),
            applied_at_utc                timestamptz,
            created_at_utc                timestamptz NOT NULL DEFAULT now(),
            updated_at_utc                timestamptz NOT NULL DEFAULT now(),
            version_no                    bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),

            PRIMARY KEY (tenant_id, workflow_task_id),
            FOREIGN KEY (tenant_id, workflow_task_id)
                REFERENCES auditcore.workflow_tasks(tenant_id, workflow_task_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE INDEX ix_delivery_vin_observation_proposals_journey
            ON auditcore.journey_delivery_vin_observation_proposals
               (tenant_id, journey_id, created_at_utc DESC);

        ALTER TABLE auditcore.journey_delivery_vin_observation_proposals
            ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.journey_delivery_vin_observation_proposals
            FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_delivery_vin_observation_proposals
            ON auditcore.journey_delivery_vin_observation_proposals
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id());
        CREATE TRIGGER trg_delivery_vin_observation_proposals_updated
            BEFORE UPDATE ON auditcore.journey_delivery_vin_observation_proposals
            FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at();
        GRANT SELECT, INSERT, UPDATE ON
            auditcore.journey_delivery_vin_observation_proposals
            TO audit_core_runtime;
        REVOKE DELETE ON auditcore.journey_delivery_vin_observation_proposals
            FROM audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS auditcore.journey_delivery_vin_observation_proposals CASCADE"
    )
