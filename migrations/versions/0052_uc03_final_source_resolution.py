"""Extend UC03 attribute resolutions for durable post-Delivery final source.

Revision ID: 0052_uc03_final_source_resolution
Revises: 0051_uc03_lossless_review_fields
Create Date: 2026-09-01

The existing journey_attribute_resolutions ledger remains the final-resolution
structure.  POST_DELIVERY resolutions need a stable value snapshot and, when the
winner is a reviewed document field, a direct reference to the durable reviewed
field row introduced/extended by 0051.

Some approved final sources are typed/source-system values rather than DI document
facts.  For those POST_DELIVERY rows the legacy DI document/field/fact columns must
be nullable; Booking/Delivery resolution rows keep their existing DI-reference
contract.  No fake DI identifiers are manufactured.
"""
from alembic import op

revision = "0052_uc03_final_source_resolution"
down_revision = "0051_uc03_lossless_review_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.journey_attribute_resolutions
            ALTER COLUMN source_di_document_id DROP NOT NULL,
            ALTER COLUMN source_field_key DROP NOT NULL,
            ALTER COLUMN source_fact_version DROP NOT NULL,
            ADD COLUMN source_reviewed_field_id uuid,
            ADD COLUMN resolved_value_snapshot jsonb;
        """
    )

    # extracted_field_id is already unique within tenant via the table primary key.
    # The redundant Journey-inclusive unique index exists only so the FK below can
    # enforce that a selected reviewed field belongs to this exact Journey as well
    # as this Tenant.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_journey_document_extracted_fields_resolution_ref
        ON auditcore.journey_document_extracted_fields
            (tenant_id, journey_id, extracted_field_id);
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_attribute_resolutions
            ADD CONSTRAINT fk_journey_attribute_resolutions_reviewed_field
            FOREIGN KEY (tenant_id, journey_id, source_reviewed_field_id)
            REFERENCES auditcore.journey_document_extracted_fields
                (tenant_id, journey_id, extracted_field_id);
        """
    )
    op.execute(
        """
        CREATE INDEX ix_journey_attribute_resolutions_reviewed_field
        ON auditcore.journey_attribute_resolutions
            (tenant_id, journey_id, source_reviewed_field_id)
        WHERE source_reviewed_field_id IS NOT NULL;
        """
    )

    # Prevent partially fabricated DI provenance. Existing BOOKING/DELIVERY rows
    # keep the old all-required contract. POST_DELIVERY may instead be backed by an
    # explicit typed/source-system owner, in which case all three DI identity fields
    # remain SQL NULL. A reviewed-field-backed final resolution must still carry the
    # copied DI identity for explainability/backward-compatible readers.
    op.execute(
        """
        ALTER TABLE auditcore.journey_attribute_resolutions
            ADD CONSTRAINT ck_journey_attribute_resolution_di_identity
            CHECK (
                (
                    source_di_document_id IS NULL
                    AND source_field_key IS NULL
                    AND source_fact_version IS NULL
                )
                OR
                (
                    source_di_document_id IS NOT NULL
                    AND source_field_key IS NOT NULL
                    AND source_fact_version IS NOT NULL
                )
            ),
            ADD CONSTRAINT ck_journey_attribute_resolution_source_contract
            CHECK (
                (
                    stage_code IN ('BOOKING','DELIVERY')
                    AND source_di_document_id IS NOT NULL
                    AND source_field_key IS NOT NULL
                    AND source_fact_version IS NOT NULL
                )
                OR
                (
                    stage_code = 'POST_DELIVERY'
                    AND (
                        (
                            source_di_document_id IS NOT NULL
                            AND source_field_key IS NOT NULL
                            AND source_fact_version IS NOT NULL
                        )
                        OR
                        (
                            source_di_document_id IS NULL
                            AND source_field_key IS NULL
                            AND source_fact_version IS NULL
                            AND source_reviewed_field_id IS NULL
                            AND owning_domain_key IS NOT NULL
                            AND owning_record_reference IS NOT NULL
                        )
                    )
                )
            ),
            ADD CONSTRAINT ck_journey_attribute_resolution_reviewed_field_identity
            CHECK (
                source_reviewed_field_id IS NULL
                OR (
                    source_di_document_id IS NOT NULL
                    AND source_field_key IS NOT NULL
                    AND source_fact_version IS NOT NULL
                )
            );
        """
    )

    op.execute(
        """
        COMMENT ON TABLE auditcore.journey_attribute_resolutions IS
        'UC03 reviewed/final attribute resolution provenance. POST_DELIVERY rows may snapshot the resolved value and optionally reference the exact durable reviewed field; typed/source-system final sources use owning domain/reference without fake DI identifiers.';
        COMMENT ON COLUMN auditcore.journey_attribute_resolutions.source_reviewed_field_id IS
        'Exact durable reviewed-field row selected as the final source when the winner is document-derived. Nullable for typed/source-system final sources.';
        COMMENT ON COLUMN auditcore.journey_attribute_resolutions.resolved_value_snapshot IS
        'Stable resolved value captured at finalization time. Required by final-source command semantics; left nullable at schema level for backward compatibility with pre-0052 rows.';
        """
    )


def downgrade() -> None:
    # A typed/source-system POST_DELIVERY resolution cannot satisfy the legacy
    # mandatory DI identity contract. Fail rather than deleting final audit state or
    # manufacturing fake document/fact identifiers.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM auditcore.journey_attribute_resolutions
                WHERE source_di_document_id IS NULL
                   OR source_field_key IS NULL
                   OR source_fact_version IS NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0052 while source-system/typed final resolutions exist';
            END IF;
        END
        $$;
        """
    )

    op.execute(
        "ALTER TABLE auditcore.journey_attribute_resolutions "
        "DROP CONSTRAINT IF EXISTS ck_journey_attribute_resolution_reviewed_field_identity"
    )
    op.execute(
        "ALTER TABLE auditcore.journey_attribute_resolutions "
        "DROP CONSTRAINT IF EXISTS ck_journey_attribute_resolution_source_contract"
    )
    op.execute(
        "ALTER TABLE auditcore.journey_attribute_resolutions "
        "DROP CONSTRAINT IF EXISTS ck_journey_attribute_resolution_di_identity"
    )
    op.execute(
        "DROP INDEX IF EXISTS auditcore.ix_journey_attribute_resolutions_reviewed_field"
    )
    op.execute(
        "ALTER TABLE auditcore.journey_attribute_resolutions "
        "DROP CONSTRAINT IF EXISTS fk_journey_attribute_resolutions_reviewed_field"
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_attribute_resolutions
            DROP COLUMN IF EXISTS resolved_value_snapshot,
            DROP COLUMN IF EXISTS source_reviewed_field_id,
            ALTER COLUMN source_di_document_id SET NOT NULL,
            ALTER COLUMN source_field_key SET NOT NULL,
            ALTER COLUMN source_fact_version SET NOT NULL;
        """
    )
    op.execute(
        "DROP INDEX IF EXISTS auditcore.uq_journey_document_extracted_fields_resolution_ref"
    )
    op.execute(
        """
        COMMENT ON TABLE auditcore.journey_attribute_resolutions IS
        'Reference-only UC03 resolution provenance. Raw DI values, confidence, page and evidence regions remain in DI and are intentionally not copied here.';
        """
    )
