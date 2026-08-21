from alembic import op

revision = "0007_uc02_product_master"
down_revision = "0006_uc02_role_mapping_ops"
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
        CREATE TABLE auditcore.project_product_masters (
            tenant_id varchar(128) NOT NULL
                REFERENCES auditcore.projects(tenant_id),
            product_master_id uuid NOT NULL DEFAULT gen_random_uuid(),
            status varchar(20) NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE','INACTIVE')),
            created_at_utc timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, product_master_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE auditcore.project_product_master_versions (
            tenant_id varchar(128) NOT NULL,
            product_master_id uuid NOT NULL,
            version_id uuid NOT NULL DEFAULT gen_random_uuid(),
            version_no integer NOT NULL CHECK (version_no > 0),
            effective_from date NOT NULL,
            lifecycle_status varchar(20) NOT NULL DEFAULT 'DRAFT'
                CHECK (lifecycle_status IN ('DRAFT','PUBLISHED','RETIRED')),
            source_import_id uuid,
            published_by_user_id varchar(160),
            published_at_utc timestamptz,
            retired_by_user_id varchar(160),
            retired_at_utc timestamptz,
            created_by_user_id varchar(160) NOT NULL,
            created_at_utc timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, product_master_id, version_id),
            UNIQUE (tenant_id, product_master_id, version_no),
            FOREIGN KEY (tenant_id, product_master_id)
                REFERENCES auditcore.project_product_masters(tenant_id, product_master_id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_project_product_master_version_id "
        "ON auditcore.project_product_master_versions (version_id)"
    )
    op.execute(
        "CREATE INDEX ix_project_product_master_effective "
        "ON auditcore.project_product_master_versions "
        "(tenant_id, lifecycle_status, effective_from DESC)"
    )
    op.execute(
        """
        CREATE TABLE auditcore.project_product_master_items (
            tenant_id varchar(128) NOT NULL,
            product_master_id uuid NOT NULL,
            version_id uuid NOT NULL,
            item_id uuid NOT NULL DEFAULT gen_random_uuid(),
            product_sku_id uuid NOT NULL
                REFERENCES auditcore.product_skus(product_sku_id),
            approved_product_snapshot jsonb NOT NULL,
            source_import_row_no integer,
            PRIMARY KEY (tenant_id, product_master_id, version_id, item_id),
            UNIQUE (tenant_id, product_master_id, version_id, product_sku_id),
            FOREIGN KEY (tenant_id, product_master_id, version_id)
                REFERENCES auditcore.project_product_master_versions(
                    tenant_id, product_master_id, version_id
                )
        )
        """
    )

    for table_name in (
        "project_product_masters",
        "project_product_master_versions",
        "project_product_master_items",
    ):
        _enable_tenant_rls(table_name)
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON auditcore.{table_name} TO {_RUNTIME_ROLE}"
        )
        op.execute(f"REVOKE DELETE ON auditcore.{table_name} FROM {_RUNTIME_ROLE}")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.protect_project_product_master_version()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            old_core jsonb;
            new_core jsonb;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.lifecycle_status = 'PUBLISHED' THEN
                    RAISE EXCEPTION 'published master version cannot be deleted';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.lifecycle_status = 'PUBLISHED' THEN
                IF NEW.lifecycle_status <> 'RETIRED' THEN
                    RAISE EXCEPTION 'published master version can only be retired';
                END IF;

                old_core := to_jsonb(OLD)
                    - 'lifecycle_status' - 'retired_at_utc' - 'retired_by_user_id';
                new_core := to_jsonb(NEW)
                    - 'lifecycle_status' - 'retired_at_utc' - 'retired_by_user_id';

                IF old_core IS DISTINCT FROM new_core THEN
                    RAISE EXCEPTION 'published master version is immutable';
                END IF;
            ELSIF OLD.lifecycle_status = 'RETIRED' THEN
                RAISE EXCEPTION 'retired master version is immutable';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_project_product_master_versions_immutable
        BEFORE UPDATE OR DELETE ON auditcore.project_product_master_versions
        FOR EACH ROW EXECUTE FUNCTION auditcore.protect_project_product_master_version()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_project_product_master_items_draft_only
        BEFORE INSERT OR UPDATE OR DELETE ON auditcore.project_product_master_items
        FOR EACH ROW EXECUTE FUNCTION auditcore.protect_version_child_mutation(
            'project_product_master_versions', 'version_id'
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_project_product_master_items_draft_only "
        "ON auditcore.project_product_master_items"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_project_product_master_versions_immutable "
        "ON auditcore.project_product_master_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.protect_project_product_master_version()")
    op.execute("DROP TABLE IF EXISTS auditcore.project_product_master_items")
    op.execute("DROP TABLE IF EXISTS auditcore.project_product_master_versions")
    op.execute("DROP TABLE IF EXISTS auditcore.project_product_masters")