from alembic import op

revision = "0014_uc02_mahindra_seg"
down_revision = "0013_uc02_project_dir"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def _enable_tenant_rls(table_name: str) -> None:
    op.execute(f"ALTER TABLE auditcore.{table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table_name}
        ON auditcore.{table_name}
        USING (tenant_id = auditcore.current_tenant_id())
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )


def upgrade() -> None:
    # Product Category is retained only as a legacy compatibility column. New UC02
    # Projects start at OEM and select one or more OEM Segments.
    op.execute("ALTER TABLE auditcore.projects ALTER COLUMN product_category_id DROP NOT NULL")

    op.execute(
        """
        CREATE TABLE auditcore.oem_segments (
            segment_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            oem_id uuid NOT NULL REFERENCES auditcore.oems(oem_id),
            segment_code varchar(100) NOT NULL,
            segment_name varchar(200) NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            created_at_utc timestamptz NOT NULL DEFAULT now(),
            updated_at_utc timestamptz NOT NULL DEFAULT now(),
            UNIQUE (oem_id, segment_code)
        )
        """
    )
    op.execute(f"GRANT SELECT ON auditcore.oem_segments TO {_RUNTIME_ROLE}")

    op.execute(
        """
        INSERT INTO auditcore.oem_segments (oem_id, segment_code, segment_name)
        SELECT oem_id, values.segment_code, values.segment_name
        FROM auditcore.oems
        CROSS JOIN (
            VALUES
                ('PASSENGER_VEHICLE', 'Passenger Vehicle'),
                ('COMMERCIAL', 'Commercial'),
                ('BATTERY_ELECTRIC', 'Battery Electric')
        ) AS values(segment_code, segment_name)
        WHERE oem_code = 'MAHINDRA'
        ON CONFLICT (oem_id, segment_code) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE TABLE auditcore.project_segments (
            tenant_id varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
            segment_id uuid NOT NULL REFERENCES auditcore.oem_segments(segment_id),
            created_by_actor_id varchar(160),
            created_at_utc timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, segment_id)
        )
        """
    )
    _enable_tenant_rls("project_segments")
    op.execute(
        f"GRANT SELECT, INSERT, DELETE ON auditcore.project_segments TO {_RUNTIME_ROLE}"
    )

    # Existing product tables remain backward compatible. For the Mahindra path,
    # product_variants represents Trim and product_configurations represents the
    # sellable drivetrain/powertrain/seating configuration below the Trim.
    op.execute(
        "ALTER TABLE auditcore.product_models ADD COLUMN segment_id uuid "
        "REFERENCES auditcore.oem_segments(segment_id)"
    )
    op.execute(
        "CREATE INDEX ix_product_models_segment ON auditcore.product_models(segment_id, model_name)"
    )
    op.execute(
        """
        CREATE TABLE auditcore.product_configurations (
            configuration_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            segment_id uuid NOT NULL REFERENCES auditcore.oem_segments(segment_id),
            model_id uuid NOT NULL REFERENCES auditcore.product_models(model_id),
            variant_id uuid NOT NULL REFERENCES auditcore.product_variants(variant_id),
            configuration_code varchar(180) NOT NULL,
            fuel_powertrain varchar(100),
            transmission varchar(100),
            drive_type varchar(80),
            seating_capacity integer CHECK (seating_capacity IS NULL OR seating_capacity > 0),
            body_type varchar(100),
            attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
            is_active boolean NOT NULL DEFAULT true,
            created_at_utc timestamptz NOT NULL DEFAULT now(),
            updated_at_utc timestamptz NOT NULL DEFAULT now(),
            UNIQUE (variant_id, configuration_code)
        )
        """
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.product_configurations TO {_RUNTIME_ROLE}"
    )
    op.execute(
        "ALTER TABLE auditcore.product_skus ADD COLUMN configuration_id uuid "
        "REFERENCES auditcore.product_configurations(configuration_id)"
    )
    op.execute(
        "CREATE INDEX ix_product_skus_configuration "
        "ON auditcore.product_skus(configuration_id)"
    )

    # Segment scope is retained with staged imports and Project Product Master
    # identities. A confirmation receipt links one uploaded OEM workbook to the
    # separate Product Master and Price List versions created from it.
    op.execute(
        "ALTER TABLE auditcore.project_master_imports ADD COLUMN segment_id uuid "
        "REFERENCES auditcore.oem_segments(segment_id)"
    )
    op.execute(
        "ALTER TABLE auditcore.project_master_imports ADD COLUMN confirmation_receipt jsonb"
    )
    op.execute(
        "CREATE INDEX ix_project_master_imports_segment "
        "ON auditcore.project_master_imports(tenant_id, master_key, segment_id, created_at_utc DESC)"
    )
    op.execute(
        "ALTER TABLE auditcore.project_product_masters ADD COLUMN segment_id uuid "
        "REFERENCES auditcore.oem_segments(segment_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_project_product_master_active_segment "
        "ON auditcore.project_product_masters(tenant_id, segment_id) "
        "WHERE status='ACTIVE' AND segment_id IS NOT NULL"
    )

    # Generic effective-dated Discount & Policy Master. Changing OEM values live
    # here; rule execution remains in the rule engine.
    op.execute(
        """
        CREATE TABLE auditcore.discount_policy_versions (
            tenant_id varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
            discount_policy_version_id uuid NOT NULL DEFAULT gen_random_uuid(),
            version_no integer NOT NULL CHECK (version_no > 0),
            effective_from date NOT NULL,
            effective_to date,
            lifecycle_status varchar(20) NOT NULL DEFAULT 'DRAFT'
                CHECK (lifecycle_status IN ('DRAFT','PUBLISHED','RETIRED')),
            source_import_id uuid,
            created_by_actor_id varchar(160) NOT NULL,
            created_at_utc timestamptz NOT NULL DEFAULT now(),
            published_by_actor_id varchar(160),
            published_at_utc timestamptz,
            retired_by_actor_id varchar(160),
            retired_at_utc timestamptz,
            updated_at_utc timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, discount_policy_version_id),
            UNIQUE (tenant_id, version_no),
            CHECK (effective_to IS NULL OR effective_to >= effective_from)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE auditcore.discount_policy_parameters (
            tenant_id varchar(128) NOT NULL,
            discount_policy_version_id uuid NOT NULL,
            parameter_id uuid NOT NULL DEFAULT gen_random_uuid(),
            scope_type varchar(30) NOT NULL
                CHECK (scope_type IN ('PROJECT','SEGMENT','MODEL','TRIM','CONFIGURATION')),
            segment_id uuid REFERENCES auditcore.oem_segments(segment_id),
            scope_key varchar(240),
            parameter_key varchar(160) NOT NULL,
            value_type varchar(20) NOT NULL
                CHECK (value_type IN ('NUMBER','TEXT','BOOLEAN')),
            value_number numeric(18,4),
            value_text text,
            unit varchar(40),
            notes text,
            source_import_row_no integer,
            PRIMARY KEY (tenant_id, parameter_id),
            FOREIGN KEY (tenant_id, discount_policy_version_id)
                REFERENCES auditcore.discount_policy_versions(
                    tenant_id, discount_policy_version_id
                ),
            CHECK (
                (value_type='NUMBER' AND value_number IS NOT NULL AND value_text IS NULL)
                OR (value_type IN ('TEXT','BOOLEAN') AND value_text IS NOT NULL AND value_number IS NULL)
            )
        )
        """
    )
    for table_name in ("discount_policy_versions", "discount_policy_parameters"):
        _enable_tenant_rls(table_name)
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON auditcore.{table_name} TO {_RUNTIME_ROLE}"
        )
        op.execute(f"REVOKE DELETE ON auditcore.{table_name} FROM {_RUNTIME_ROLE}")

    op.execute(
        """
        CREATE TRIGGER trg_discount_policy_versions_immutable
        BEFORE UPDATE OR DELETE ON auditcore.discount_policy_versions
        FOR EACH ROW EXECUTE FUNCTION auditcore.protect_published_version()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_discount_policy_parameters_draft_only
        BEFORE INSERT OR UPDATE OR DELETE ON auditcore.discount_policy_parameters
        FOR EACH ROW EXECUTE FUNCTION auditcore.protect_version_child_mutation(
            'discount_policy_versions', 'discount_policy_version_id'
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_discount_policy_parameters_draft_only "
        "ON auditcore.discount_policy_parameters"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_discount_policy_versions_immutable "
        "ON auditcore.discount_policy_versions"
    )
    op.execute("DROP TABLE IF EXISTS auditcore.discount_policy_parameters")
    op.execute("DROP TABLE IF EXISTS auditcore.discount_policy_versions")
    op.execute("DROP INDEX IF EXISTS auditcore.uq_project_product_master_active_segment")
    op.execute("ALTER TABLE auditcore.project_product_masters DROP COLUMN IF EXISTS segment_id")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_project_master_imports_segment")
    op.execute("ALTER TABLE auditcore.project_master_imports DROP COLUMN IF EXISTS confirmation_receipt")
    op.execute("ALTER TABLE auditcore.project_master_imports DROP COLUMN IF EXISTS segment_id")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_product_skus_configuration")
    op.execute("ALTER TABLE auditcore.product_skus DROP COLUMN IF EXISTS configuration_id")
    op.execute("DROP TABLE IF EXISTS auditcore.product_configurations")
    op.execute("DROP INDEX IF EXISTS auditcore.ix_product_models_segment")
    op.execute("ALTER TABLE auditcore.product_models DROP COLUMN IF EXISTS segment_id")
    op.execute("DROP TABLE IF EXISTS auditcore.project_segments")
    op.execute("DROP TABLE IF EXISTS auditcore.oem_segments")
    op.execute("ALTER TABLE auditcore.projects ALTER COLUMN product_category_id SET NOT NULL")
