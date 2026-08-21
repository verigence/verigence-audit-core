from alembic import op

revision = "0008_uc02_project_master_imports"
down_revision = "0007_uc02_product_master"
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
    op.execute(
        """
        CREATE TABLE auditcore.project_master_imports (
            import_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
            owner_module varchar(40) NOT NULL
                CHECK (owner_module IN ('AUDIT_CORE','DI')),
            master_key varchar(120) NOT NULL,
            effective_from date,
            template_version varchar(80),
            original_file_name varchar(255) NOT NULL,
            file_hash varchar(64) NOT NULL,
            idempotency_key varchar(200) NOT NULL,
            semantic_request_hash varchar(64) NOT NULL,
            status varchar(30) NOT NULL DEFAULT 'UPLOADED'
                CHECK (status IN (
                    'UPLOADED','PARSING','PREVIEW_READY','VALIDATION_FAILED',
                    'CONFIRMED','CANCELLED','FAILED'
                )),
            rows_parsed integer NOT NULL DEFAULT 0 CHECK (rows_parsed >= 0),
            valid_rows integer NOT NULL DEFAULT 0 CHECK (valid_rows >= 0),
            warning_rows integer NOT NULL DEFAULT 0 CHECK (warning_rows >= 0),
            error_rows integer NOT NULL DEFAULT 0 CHECK (error_rows >= 0),
            confirmed_version_id uuid,
            created_by_user_id varchar(160) NOT NULL,
            created_at_utc timestamptz NOT NULL DEFAULT now(),
            confirmed_by_user_id varchar(160),
            confirmed_at_utc timestamptz,
            version_no bigint NOT NULL DEFAULT 1 CHECK (version_no > 0),
            UNIQUE (tenant_id, owner_module, master_key, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_project_master_imports_tenant_master_status "
        "ON auditcore.project_master_imports "
        "(tenant_id, owner_module, master_key, status, created_at_utc DESC)"
    )
    op.execute(
        """
        CREATE TABLE auditcore.project_master_import_rows (
            tenant_id varchar(128) NOT NULL,
            import_id uuid NOT NULL,
            row_number integer NOT NULL CHECK (row_number > 0),
            parsed_data jsonb NOT NULL,
            validation_status varchar(20) NOT NULL
                CHECK (validation_status IN ('VALID','WARNING','ERROR')),
            validation_messages jsonb NOT NULL DEFAULT '[]'::jsonb,
            PRIMARY KEY (tenant_id, import_id, row_number),
            FOREIGN KEY (import_id)
                REFERENCES auditcore.project_master_imports(import_id)
                ON DELETE CASCADE,
            FOREIGN KEY (tenant_id)
                REFERENCES auditcore.projects(tenant_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_project_master_import_rows_validation "
        "ON auditcore.project_master_import_rows "
        "(tenant_id, import_id, validation_status, row_number)"
    )

    for table_name in (
        "project_master_imports",
        "project_master_import_rows",
    ):
        _enable_tenant_rls(table_name)
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON auditcore.{table_name} TO {_RUNTIME_ROLE}"
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.project_master_import_rows")
    op.execute("DROP TABLE IF EXISTS auditcore.project_master_imports")
