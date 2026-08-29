"""Add reference-only UC03 attribute resolution provenance.

Revision ID: 0037_uc03_attribute_resolution_refs
Revises: 0036_uc03_document_capture_v2
Create Date: 2026-08-30

Audit Core must not duplicate DI's raw extracted values, confidence, page or evidence
regions. This table stores only the source references used when a reviewed UC03
business attribute is committed to an Audit Core typed domain.
"""
from alembic import op

revision = "0037_uc03_attribute_resolution_refs"
down_revision = "0036_uc03_document_capture_v2"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.journey_attribute_resolutions (
            tenant_id                 varchar(128) NOT NULL,
            journey_attribute_resolution_id uuid NOT NULL DEFAULT gen_random_uuid(),
            journey_id                uuid NOT NULL,
            stage_code                varchar(30) NOT NULL
                                      CHECK (stage_code IN ('BOOKING','DELIVERY','POST_DELIVERY')),
            attribute_key             varchar(160) NOT NULL,
            excel_field_no            integer,
            mapping_status            varchar(20) NOT NULL
                                      CHECK (mapping_status IN ('SUPPORTED','PROVISIONAL')),
            source_di_document_id      uuid NOT NULL,
            source_evidence_id         uuid,
            source_canonical_field_id  varchar(160),
            source_field_key           varchar(160) NOT NULL,
            source_fact_version        integer NOT NULL CHECK (source_fact_version > 0),
            source_document_type_key   varchar(120),
            resolution_rule            varchar(80) NOT NULL,
            mapping_version            varchar(40) NOT NULL,
            owning_domain_key          varchar(100),
            owning_record_reference    varchar(240),
            resolved_by_actor_id       varchar(160) NOT NULL,
            resolved_at_utc            timestamptz NOT NULL DEFAULT now(),
            created_at_utc             timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_attribute_resolution_id),
            UNIQUE (tenant_id, journey_id, stage_code, attribute_key),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, source_evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_journey_attribute_resolutions_source
        ON auditcore.journey_attribute_resolutions
            (tenant_id, journey_id, source_di_document_id, source_field_key)
        """
    )
    op.execute(
        "ALTER TABLE auditcore.journey_attribute_resolutions ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE auditcore.journey_attribute_resolutions FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation_journey_attribute_resolutions
        ON auditcore.journey_attribute_resolutions
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT ON auditcore.journey_attribute_resolutions TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE UPDATE, DELETE ON auditcore.journey_attribute_resolutions FROM {_RUNTIME_ROLE}"
    )
    op.execute(
        """
        COMMENT ON TABLE auditcore.journey_attribute_resolutions IS
        'Reference-only UC03 resolution provenance. Raw DI values, confidence, page and evidence regions remain in DI and are intentionally not copied here.'
        """
    )
    op.execute(
        """
        COMMENT ON TABLE auditcore.journey_document_extracted_fields IS
        'Legacy V1 correction ledger. New common review flows must not copy unchanged DI extracted values into this table; DI remains source of truth for machine facts.'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.journey_attribute_resolutions")
