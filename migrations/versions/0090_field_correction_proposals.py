"""0090_field_correction_proposals — >=90%-confidence correction approval
flow (unified Documents review redesign, 2026-09-13).

Booking/Delivery's own Confirm gates now relax to "document completeness is
the sole criterion to finish" (see 0089's sibling code changes). A field
extracted at >=90% confidence that is nonetheless WRONG can no longer be
edited directly on the old Review screen -- direct editing at high confidence
was deliberately rejected in favour of a structured, TL-adjudicated
correction: PC (or TL) proposes a replacement value for one field on one
document, which raises a VIOLATION finding (routed through the existing
finding-classification/routing/act_on_audit_flag machinery, no new action
codes) for a Lead to Confirm-Breach (apply it) or Mark-False-Positive
(reject it, leaving the original DI value untouched).

One row per proposed correction, keyed by its own audit_finding_id (1:1 with
the finding it raised) rather than by (journey, field) -- a field can be
proposed, rejected, and re-proposed, and each attempt is its own finding with
its own trail. ``original_value``/``proposed_value`` are jsonb to hold
whatever shape the source field carries (string, number, structured).

Applying an approved proposal reuses ``persist_reviewed_di_fields()``
(uc03_di_core_persistence.py) -- the same write path 5 existing Confirm
handlers already use -- rather than a new write mechanism; this table is
purely the proposal/audit record, not a second copy of extracted-field
storage.
"""

from alembic import op

revision = "0090_field_correction_proposals"
down_revision = "0089_dup_booking_field_index"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.journey_document_field_correction_proposals (
            tenant_id                varchar(128) NOT NULL,
            audit_finding_id         uuid NOT NULL,
            journey_id               uuid NOT NULL,
            stage_code               varchar(20) NOT NULL
                                     CHECK (stage_code IN ('BOOKING','DELIVERY')),
            document_id              uuid NOT NULL,
            evidence_id              uuid,
            document_type_key        varchar(80) NOT NULL,
            field_key                varchar(160) NOT NULL,
            canonical_field_id       varchar(160) NOT NULL,
            source_fact_version      integer NOT NULL,
            confidence_score         numeric(6,3),
            original_value           jsonb,
            proposed_value           jsonb NOT NULL,
            proposed_by_actor_id     varchar(160) NOT NULL,
            applied_at_utc           timestamptz,
            created_at_utc           timestamptz NOT NULL DEFAULT now(),
            updated_at_utc           timestamptz NOT NULL DEFAULT now(),
            version_no               bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),

            PRIMARY KEY (tenant_id, audit_finding_id),
            FOREIGN KEY (tenant_id, audit_finding_id)
                REFERENCES auditcore.audit_findings(tenant_id, audit_finding_id),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id)
        );

        CREATE INDEX ix_document_field_correction_proposals_journey
            ON auditcore.journey_document_field_correction_proposals
               (tenant_id, journey_id, document_id, created_at_utc DESC);

        ALTER TABLE auditcore.journey_document_field_correction_proposals
            ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.journey_document_field_correction_proposals
            FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation_document_field_correction_proposals
            ON auditcore.journey_document_field_correction_proposals
            USING (tenant_id = auditcore.current_tenant_id())
            WITH CHECK (tenant_id = auditcore.current_tenant_id());
        CREATE TRIGGER trg_document_field_correction_proposals_updated
            BEFORE UPDATE ON auditcore.journey_document_field_correction_proposals
            FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at();
        GRANT SELECT, INSERT, UPDATE ON
            auditcore.journey_document_field_correction_proposals
            TO audit_core_runtime;
        REVOKE DELETE ON auditcore.journey_document_field_correction_proposals
            FROM audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS auditcore.journey_document_field_correction_proposals CASCADE"
    )
