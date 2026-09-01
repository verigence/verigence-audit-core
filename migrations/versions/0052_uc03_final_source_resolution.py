"""Extend UC03 attribute resolutions for durable post-Delivery final source.

Revision ID: 0052_uc03_final_source_resolution
Revises: 0051_uc03_lossless_review_fields
Create Date: 2026-09-01

The existing journey_attribute_resolutions ledger remains the sparse document-
derived final-resolution structure. POST_DELIVERY resolutions need a stable value
snapshot and a direct reference to the exact durable reviewed field selected from
Booking or Delivery.

Typed/source-system report fields continue to come directly from their existing
Audit Core business owners and are not duplicated into this ledger. Existing DI
source-identity requirements therefore remain unchanged.
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
            ADD COLUMN source_reviewed_field_id uuid,
            ADD COLUMN resolved_value_snapshot jsonb;
        """
    )

    # extracted_field_id is already unique within tenant via the table primary key.
    # The Journey-inclusive unique index exists so the FK below also proves that the
    # selected reviewed field belongs to this exact Journey.
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

    op.execute(
        """
        COMMENT ON TABLE auditcore.journey_attribute_resolutions IS
        'UC03 document-derived attribute resolution provenance. POST_DELIVERY rows may reference the exact durable reviewed field and snapshot its resolved effective value; typed/source-system report fields remain in their existing domain owners.';
        COMMENT ON COLUMN auditcore.journey_attribute_resolutions.source_reviewed_field_id IS
        'Exact durable Booking/Delivery reviewed-field row selected for a document-derived final source. Nullable for pre-0052 resolution rows.';
        COMMENT ON COLUMN auditcore.journey_attribute_resolutions.resolved_value_snapshot IS
        'Stable resolved effective value captured at finalization time. Nullable for pre-0052 resolution rows.';
        """
    )


def downgrade() -> None:
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
            DROP COLUMN IF EXISTS source_reviewed_field_id;
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
