from alembic import op

revision = "0009_uc02_project_reference_masters"
down_revision = "0008_uc02_project_master_imports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO auditcore.oems (oem_id, oem_code, oem_name, is_active)
        VALUES
            ('10000000-0000-4000-8000-000000000001', 'MAHINDRA', 'Mahindra', true),
            ('10000000-0000-4000-8000-000000000002', 'HYUNDAI', 'Hyundai', true),
            ('10000000-0000-4000-8000-000000000003', 'MARUTI', 'Maruti', true),
            ('10000000-0000-4000-8000-000000000004', 'MERCEDES_BENZ', 'Mercedes Benz', true),
            ('10000000-0000-4000-8000-000000000005', 'BMW', 'BMW', true),
            ('10000000-0000-4000-8000-000000000006', 'SKODA', 'Skoda', true),
            ('10000000-0000-4000-8000-000000000007', 'VOLKSWAGEN', 'Volkswagen', true),
            ('10000000-0000-4000-8000-000000000008', 'TATA_MOTORS', 'Tata Motors', true)
        ON CONFLICT (oem_code) DO UPDATE
        SET oem_name = EXCLUDED.oem_name,
            is_active = true,
            updated_at_utc = now()
        """
    )
    op.execute(
        """
        INSERT INTO auditcore.product_categories (
            product_category_id, category_code, category_name, is_active
        )
        VALUES (
            '20000000-0000-4000-8000-000000000001',
            'FOUR_WHEELERS',
            'Four Wheelers',
            true
        )
        ON CONFLICT (category_code) DO UPDATE
        SET category_name = EXCLUDED.category_name,
            is_active = true,
            updated_at_utc = now()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM auditcore.oems o
        WHERE o.oem_code IN (
            'MAHINDRA', 'HYUNDAI', 'MARUTI', 'MERCEDES_BENZ',
            'BMW', 'SKODA', 'VOLKSWAGEN', 'TATA_MOTORS'
        )
          AND NOT EXISTS (
              SELECT 1 FROM auditcore.projects p WHERE p.oem_id = o.oem_id
          )
        """
    )
    op.execute(
        """
        DELETE FROM auditcore.product_categories c
        WHERE c.category_code = 'FOUR_WHEELERS'
          AND NOT EXISTS (
              SELECT 1 FROM auditcore.projects p
              WHERE p.product_category_id = c.product_category_id
          )
        """
    )
