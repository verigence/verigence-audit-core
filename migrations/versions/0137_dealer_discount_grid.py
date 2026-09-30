"""0137_dealer_discount_grid — the dealer's discount grid as the fifth master.

Per tenant (project), effective-dated like the other masters (decision
2026-09-30): one row per model with booking protection days, the agreed
buffer, the insurance OD percentage (a maximum for now) and the
out-of-territory addition, plus the policy parameters as written. A row
whose model the catalogue does not know yet keeps its alias and is
reported unresolved, never guessed.
"""
from __future__ import annotations

from alembic import op

revision = "0137_dealer_discount_grid"
down_revision = "0136_uc03_price_since"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.dealer_discount_grid_versions (
            tenant_id            varchar(128) NOT NULL REFERENCES auditcore.projects(tenant_id),
            grid_version_id      uuid         NOT NULL DEFAULT gen_random_uuid(),
            version_no           integer      NOT NULL CHECK (version_no > 0),
            lifecycle_status     varchar(20)  NOT NULL DEFAULT 'PUBLISHED'
                                 CHECK (lifecycle_status IN ('PUBLISHED','RETIRED')),
            effective_from       date         NOT NULL,
            effective_to         date,
            source_upload_id     uuid,
            parameters           jsonb        NOT NULL DEFAULT '[]'::jsonb,
            created_by_actor_id  varchar(160),
            created_at_utc       timestamptz  NOT NULL DEFAULT now(),
            updated_at_utc       timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, grid_version_id),
            UNIQUE (tenant_id, version_no),
            CHECK (effective_to IS NULL OR effective_to >= effective_from)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE auditcore.dealer_discount_grid_rows (
            tenant_id                varchar(128) NOT NULL,
            grid_version_id          uuid         NOT NULL,
            grid_row_id              uuid         NOT NULL DEFAULT gen_random_uuid(),
            model_alias              varchar(240) NOT NULL,
            model_id                 uuid REFERENCES auditcore.product_models(model_id),
            in_scope                 boolean      NOT NULL DEFAULT true,
            booking_protection_days  integer,
            agreed_buffer_amount     numeric(18,2),
            insurance_od_percent     numeric(9,4),
            out_of_territory_amount  numeric(18,2),
            raw                      jsonb        NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (tenant_id, grid_row_id),
            FOREIGN KEY (tenant_id, grid_version_id)
                REFERENCES auditcore.dealer_discount_grid_versions(tenant_id, grid_version_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_dealer_discount_grid_rows_model "
        "ON auditcore.dealer_discount_grid_rows (tenant_id, grid_version_id, model_id)"
    )
    op.execute(
        "CREATE TRIGGER trg_dealer_discount_grid_versions_updated_at "
        "BEFORE UPDATE ON auditcore.dealer_discount_grid_versions "
        "FOR EACH ROW EXECUTE FUNCTION auditcore.set_updated_at()"
    )
    for table_name in ("dealer_discount_grid_versions", "dealer_discount_grid_rows"):
        op.execute(f"ALTER TABLE auditcore.{table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE auditcore.{table_name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation_{table_name} ON auditcore.{table_name} "
            "USING (tenant_id = auditcore.current_tenant_id()) "
            "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{table_name} TO {_RUNTIME_ROLE}")
        op.execute(f"REVOKE DELETE ON auditcore.{table_name} FROM {_RUNTIME_ROLE}")

    # The upload log accepts the fifth master kind.
    op.execute("ALTER TABLE auditcore.oem_master_uploads DROP CONSTRAINT oem_master_uploads_master_kind_check")
    op.execute(
        """
        ALTER TABLE auditcore.oem_master_uploads ADD CONSTRAINT oem_master_uploads_master_kind_check
        CHECK (master_kind IN ('PRICE_LIST','CONSUMER_SCHEME','EXCHANGE_SCHEME','CORPORATE_POLICY','DISCOUNT_GRID'))
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.oem_master_uploads DROP CONSTRAINT oem_master_uploads_master_kind_check")
    op.execute(
        """
        ALTER TABLE auditcore.oem_master_uploads ADD CONSTRAINT oem_master_uploads_master_kind_check
        CHECK (master_kind IN ('PRICE_LIST','CONSUMER_SCHEME','EXCHANGE_SCHEME','CORPORATE_POLICY'))
        """
    )
    op.execute("DROP TABLE IF EXISTS auditcore.dealer_discount_grid_rows")
    op.execute("DROP TABLE IF EXISTS auditcore.dealer_discount_grid_versions")
