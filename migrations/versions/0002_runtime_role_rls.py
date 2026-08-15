from alembic import op

revision = "0002_runtime_role_rls"
down_revision = "0001_vac_db_002"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_RUNTIME_ROLE}') THEN
                CREATE ROLE {_RUNTIME_ROLE}
                    NOLOGIN
                    NOSUPERUSER
                    NOCREATEDB
                    NOCREATEROLE
                    NOBYPASSRLS;
            END IF;
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA auditcore TO {_RUNTIME_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA auditcore TO {_RUNTIME_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auditcore TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON ALL TABLES IN SCHEMA auditcore FROM {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA auditcore FROM {_RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA auditcore FROM {_RUNTIME_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA auditcore FROM {_RUNTIME_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {_RUNTIME_ROLE}")
