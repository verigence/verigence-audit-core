from alembic import op

revision = "0009_uc02_project_reference_masters"
down_revision = "0008_uc02_project_master_imports"
branch_labels = None
depends_on = None


_OEM_IDS = (
    "10000000-0000-4000-8000-000000000001",
    "10000000-0000-4000-8000-000000000002",
    "10000000-0000-4000-8000-000000000003",
    "10000000-0000-4000-8000-000000000004",
    "10000000-0000-4000-8000-000000000005",
    "10000000-0000-4000-8000-000000000006",
    "10000000-0000-4000-8000-000000000007",
    "10000000-0000-4000-8000-000000000008",
)
_PRODUCT_CATEGORY_ID = "20000000-0000-4000-8000-000000000001"


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
    # Do not delete or deactivate a pre-existing reference row that was matched by
    # business code during upgrade. Only remove rows created with this migration's
    # deterministic identifiers, and only when no Project references them.
    for oem_id in _OEM_IDS:
        op.execute(
            f"""
            DELETE FROM auditcore.oems o
            WHERE o.oem_id = '{oem_id}'::uuid
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.projects p WHERE p.oem_id = o.oem_id
              )
            """
        )
    op.execute(
        f"""
        DELETE FROM auditcore.product_categories c
        WHERE c.product_category_id = '{_PRODUCT_CATEGORY_ID}'::uuid
          AND NOT EXISTS (
              SELECT 1 FROM auditcore.projects p
              WHERE p.product_category_id = c.product_category_id
          )
        """
    )
