"""Extend generic UC03 reviewed DI fields for lossless Booking/Delivery persistence.

Revision ID: 0051_uc03_lossless_review_fields
Revises: 0050_uc03_commercial_components
Create Date: 2026-09-01

The existing journey_document_extracted_fields table remains the generic durable
reviewed-field store. This migration extends it for current Schema V2 identifiers
and Delivery stage provenance without creating a parallel raw-field table.

Legacy V1 rows keep their evidence/source_fact_ref identity. Current V2 rows may
instead use DI document + canonical field + fact version identity, so the two
legacy identifiers become nullable. Confidence values are stored without guessing
or rescaling; confidence_scale records whether a source supplied UNIT_INTERVAL
(0..1) or PERCENT (0..100) values.
"""
from alembic import op

revision = "0051_uc03_lossless_review_fields"
down_revision = "0050_uc03_commercial_components"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.journey_document_extracted_fields
            ALTER COLUMN evidence_id DROP NOT NULL,
            ALTER COLUMN source_fact_ref DROP NOT NULL,
            DROP CONSTRAINT IF EXISTS journey_document_extracted_fields_confidence_score_check,
            ALTER COLUMN confidence_score TYPE numeric(10,7)
                USING confidence_score::numeric(10,7),
            ADD COLUMN stage_code varchar(30) NOT NULL DEFAULT 'BOOKING',
            ADD COLUMN source_document_type_key varchar(120),
            ADD COLUMN source_canonical_field_id varchar(160),
            ADD COLUMN effective_value jsonb,
            ADD COLUMN confidence_scale varchar(20),
            ADD COLUMN is_modified boolean NOT NULL DEFAULT false,
            ADD COLUMN reviewed_by_actor_id varchar(160),
            ADD COLUMN reviewed_at_utc timestamptz;
        """
    )
    op.execute(
        """
        UPDATE auditcore.journey_document_extracted_fields
        SET effective_value = COALESCE(modified_value, extracted_value),
            is_modified = (modified_value IS NOT NULL),
            confidence_scale = CASE
                WHEN confidence_score IS NOT NULL THEN 'UNIT_INTERVAL'
                ELSE NULL
            END,
            reviewed_by_actor_id = COALESCE(modified_by_actor_id, reviewed_by_actor_id),
            reviewed_at_utc = COALESCE(modified_at_utc, reviewed_at_utc)
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_document_extracted_fields
            ADD CONSTRAINT ck_journey_document_extracted_fields_stage
                CHECK (stage_code IN ('BOOKING','DELIVERY')),
            ADD CONSTRAINT ck_journey_document_extracted_fields_confidence
                CHECK (
                    (confidence_score IS NULL AND confidence_scale IS NULL)
                    OR
                    (
                        confidence_score IS NOT NULL
                        AND confidence_score >= 0
                        AND confidence_score <= 100
                        AND confidence_scale IN ('UNIT_INTERVAL','PERCENT')
                        AND (
                            confidence_scale <> 'UNIT_INTERVAL'
                            OR confidence_score <= 1
                        )
                    )
                );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_journey_document_extracted_fields_v2_fact
        ON auditcore.journey_document_extracted_fields (
            tenant_id, journey_id, stage_code, di_document_id,
            source_canonical_field_id, source_fact_version
        )
        WHERE source_canonical_field_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_journey_document_extracted_fields_stage
        ON auditcore.journey_document_extracted_fields (
            tenant_id, journey_id, stage_code, di_document_id, field_key
        )
        """
    )
    op.execute(
        """
        COMMENT ON TABLE auditcore.journey_document_extracted_fields IS
        'Durable UC03 reviewed DI fields for Booking/Delivery. Keeps original DI value/provenance and the reviewed effective value; typed business projection remains additional.';
        COMMENT ON COLUMN auditcore.journey_document_extracted_fields.effective_value IS
        'Reviewed effective value. For accepted unchanged fields this equals extracted_value; for accepted corrections it is the confirmed modified value; may be SQL NULL when no effective business value was accepted.';
        COMMENT ON COLUMN auditcore.journey_document_extracted_fields.source_canonical_field_id IS
        'Schema V2 DI canonical field identifier. Together with DI document and fact version it provides stable V2 field identity when legacy source_fact_ref is unavailable.';
        COMMENT ON COLUMN auditcore.journey_document_extracted_fields.confidence_scale IS
        'Scale of confidence_score: UNIT_INTERVAL (0..1) or PERCENT (0..100). Values are not guessed or rescaled.';
        """
    )


def downgrade() -> None:
    # Rows that rely on V2-only identity/stage semantics cannot satisfy the legacy
    # NOT NULL source_fact_ref/evidence_id contract. Removing them is the only
    # truthful downgrade; no fake legacy UUIDs are manufactured.
    op.execute(
        """
        DELETE FROM auditcore.journey_document_extracted_fields
        WHERE evidence_id IS NULL
           OR source_fact_ref IS NULL
           OR stage_code <> 'BOOKING'
           OR confidence_score > 1
        """
    )
    op.execute("DROP INDEX IF EXISTS auditcore.ix_journey_document_extracted_fields_stage")
    op.execute("DROP INDEX IF EXISTS auditcore.uq_journey_document_extracted_fields_v2_fact")
    op.execute(
        """
        ALTER TABLE auditcore.journey_document_extracted_fields
            DROP CONSTRAINT IF EXISTS ck_journey_document_extracted_fields_confidence,
            DROP CONSTRAINT IF EXISTS ck_journey_document_extracted_fields_stage,
            DROP COLUMN IF EXISTS reviewed_at_utc,
            DROP COLUMN IF EXISTS reviewed_by_actor_id,
            DROP COLUMN IF EXISTS is_modified,
            DROP COLUMN IF EXISTS confidence_scale,
            DROP COLUMN IF EXISTS effective_value,
            DROP COLUMN IF EXISTS source_canonical_field_id,
            DROP COLUMN IF EXISTS source_document_type_key,
            DROP COLUMN IF EXISTS stage_code,
            ALTER COLUMN confidence_score TYPE numeric(8,7)
                USING confidence_score::numeric(8,7),
            ALTER COLUMN source_fact_ref SET NOT NULL,
            ALTER COLUMN evidence_id SET NOT NULL,
            ADD CONSTRAINT journey_document_extracted_fields_confidence_score_check
                CHECK (
                    confidence_score IS NULL
                    OR (confidence_score >= 0 AND confidence_score <= 1)
                );
        """
    )
    op.execute(
        """
        COMMENT ON TABLE auditcore.journey_document_extracted_fields IS
        'Legacy V1 correction ledger. New common review flows must not copy unchanged DI extracted values into this table; DI remains source of truth for machine facts.'
        """
    )
