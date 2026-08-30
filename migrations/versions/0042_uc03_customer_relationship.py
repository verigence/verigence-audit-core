"""Add reviewed UC03 customer relationship fields.

Revision ID: 0042_uc03_customer_relationship
Revises: 0041_uc03_stage_linkage
Create Date: 2026-08-30

PAN and Aadhaar source-specific relationship facts remain in DI. Audit Core stores
only the reviewed/resolved Customer relationship value for operational/audit use;
journey_attribute_resolutions retains the accepted source provenance.
"""
from alembic import op

revision = "0042_uc03_customer_relationship"
down_revision = "0041_uc03_stage_linkage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.customers
            ADD COLUMN relationship_type varchar(3),
            ADD COLUMN relationship_name varchar(240),
            ADD CONSTRAINT ck_customers_relationship_type
                CHECK (relationship_type IS NULL OR relationship_type IN ('S/O','W/O','D/O'));

        COMMENT ON COLUMN auditcore.customers.relationship_type IS
            'Reviewed explicit relationship marker from identity evidence: S/O, W/O or D/O. Never inferred from an unlabeled parent/spouse name.';
        COMMENT ON COLUMN auditcore.customers.relationship_name IS
            'Reviewed related-person name associated with the explicit identity relationship marker. Source-specific raw facts remain in DI.';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.customers
            DROP CONSTRAINT IF EXISTS ck_customers_relationship_type,
            DROP COLUMN IF EXISTS relationship_name,
            DROP COLUMN IF EXISTS relationship_type;
        """
    )