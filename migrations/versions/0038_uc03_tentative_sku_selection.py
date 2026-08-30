from alembic import op

revision = "0038_uc03_tentative_sku_selection"
down_revision = "0037_uc03_attribute_resolution_refs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.journey_products
            ADD COLUMN selection_status varchar(30),
            ADD COLUMN selection_method varchar(80),
            ADD COLUMN selection_score numeric(8,7)
                CHECK (selection_score IS NULL OR (selection_score >= 0 AND selection_score <= 1))
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_products
        ADD CONSTRAINT ck_journey_products_selection_status
        CHECK (selection_status IS NULL OR selection_status IN ('TENTATIVE','CONFIRMED'))
        """
    )

    # Existing SKU selections pre-date tentative inference and therefore retain
    # their explicit/operational meaning rather than being downgraded to tentative.
    op.execute(
        """
        UPDATE auditcore.journey_products
        SET selection_status='CONFIRMED'
        WHERE product_sku_id IS NOT NULL
          AND selection_status IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.journey_products "
        "DROP CONSTRAINT IF EXISTS ck_journey_products_selection_status"
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_products
            DROP COLUMN IF EXISTS selection_score,
            DROP COLUMN IF EXISTS selection_method,
            DROP COLUMN IF EXISTS selection_status
        """
    )
