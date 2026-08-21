from alembic import op

revision = "0006_uc02_role_mapping_ops"
down_revision = "0005_uc02_admin_row_delete"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditcore.administrative_operations (
            operation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            operation_type varchar(40) NOT NULL
                CHECK (operation_type IN ('PROJECT_PROVISION','ROLE_MAPPING','PROJECT_DELETE')),
            tenant_id varchar(128),
            idempotency_key varchar(200) NOT NULL,
            semantic_request_hash varchar(64) NOT NULL,
            status varchar(30) NOT NULL
                CHECK (status IN ('RECEIVED','RUNNING','RECOVERY_REQUIRED','COMPLETED','FAILED')),
            current_step varchar(40),
            initiated_by_user_id varchar(160) NOT NULL,
            correlation_id varchar(160),
            safe_request_summary jsonb,
            security_receipt jsonb,
            audit_core_receipt jsonb,
            di_receipt jsonb,
            last_error_code varchar(100),
            last_error_summary varchar(500),
            created_at_utc timestamptz NOT NULL DEFAULT now(),
            updated_at_utc timestamptz NOT NULL DEFAULT now(),
            completed_at_utc timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_admin_operation_idempotency
        ON auditcore.administrative_operations (
            operation_type,
            COALESCE(tenant_id, ''),
            idempotency_key
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_admin_operation_tenant_status "
        "ON auditcore.administrative_operations (tenant_id, operation_type, status)"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.administrative_operations TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE DELETE ON auditcore.administrative_operations FROM {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.administrative_operations")
