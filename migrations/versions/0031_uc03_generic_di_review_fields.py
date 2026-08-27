"""Store every DI extraction used by PC document review.

Revision ID: 0031_uc03_generic_review_fields
Revises: 0030_uc03_customer_mobile_pii
Create Date: 2026-08-27

The PC Review screen reads extraction directly from DI. Audit Core persists the
complete reviewed document as generic fields so new DI fields do not require an
Audit Core schema change. Only fields modified by the PC carry modification
audit columns; unchanged fields retain modified_value as SQL NULL.
"""
from alembic import op

revision = "0031_uc03_generic_review_fields"
down_revision = "0030_uc03_customer_mobile_pii"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.journey_document_extracted_fields (
            tenant_id               varchar(128) NOT NULL,
            journey_id              uuid NOT NULL,
            evidence_id             uuid NOT NULL,
            di_document_id          uuid NOT NULL,
            extracted_field_id      uuid NOT NULL DEFAULT gen_random_uuid(),
            source_fact_ref         uuid NOT NULL,
            source_fact_version     integer NOT NULL DEFAULT 1
                                    CHECK (source_fact_version > 0),
            field_key               varchar(160) NOT NULL,
            extracted_value         jsonb,
            modified_value          jsonb,
            confidence_score        numeric(8,7)
                                    CHECK (
                                        confidence_score IS NULL
                                        OR (confidence_score >= 0 AND confidence_score <= 1)
                                    ),
            modified_by_actor_id    varchar(160),
            modified_at_utc         timestamptz,
            created_at_utc          timestamptz NOT NULL DEFAULT now(),
            updated_at_utc          timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, extracted_field_id),
            UNIQUE (
                tenant_id, journey_id, di_document_id,
                source_fact_ref, source_fact_version
            ),
            FOREIGN KEY (tenant_id, journey_id)
                REFERENCES auditcore.journeys(tenant_id, journey_id),
            FOREIGN KEY (tenant_id, evidence_id)
                REFERENCES auditcore.evidence(tenant_id, evidence_id),
            CHECK (
                (modified_value IS NULL
                 AND modified_by_actor_id IS NULL
                 AND modified_at_utc IS NULL)
                OR
                (modified_value IS NOT NULL
                 AND modified_by_actor_id IS NOT NULL
                 AND modified_at_utc IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_journey_document_extracted_fields_lookup
        ON auditcore.journey_document_extracted_fields
            (tenant_id, journey_id, di_document_id, field_key)
        """
    )
    op.execute(
        "ALTER TABLE auditcore.journey_document_extracted_fields ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE auditcore.journey_document_extracted_fields FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation_journey_document_extracted_fields
        ON auditcore.journey_document_extracted_fields
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journey_document_extracted_fields_updated
        BEFORE UPDATE ON auditcore.journey_document_extracted_fields
        FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.journey_document_extracted_fields TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE DELETE ON auditcore.journey_document_extracted_fields FROM {_RUNTIME_ROLE}"
    )
    op.execute(
        "COMMENT ON TABLE auditcore.journey_document_extracted_fields IS "
        "'Generic PC-reviewed DI extraction values. modified_value is populated only when the PC changes the DI value.'"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_journey_document_extracted_fields_updated "
        "ON auditcore.journey_document_extracted_fields"
    )
    op.execute("DROP TABLE IF EXISTS auditcore.journey_document_extracted_fields")
