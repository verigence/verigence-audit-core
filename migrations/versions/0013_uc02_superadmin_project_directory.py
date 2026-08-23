from alembic import op

revision = "0013_uc02_project_dir"
down_revision = "0012_uc03_delivery_capture"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # UC02 Project Administration needs a cross-Tenant Project directory, but only
    # after Security has attested the initiating human as platform SuperAdmin. Keep
    # the runtime role NOBYPASSRLS and express that narrow read through RLS rather
    # than querying as the schema owner or joining against Security Tenant listings.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.current_platform_super_admin()
        RETURNS boolean
        LANGUAGE sql
        STABLE
        AS $$
            SELECT COALESCE(
                NULLIF(current_setting('app.platform_super_admin', true), '')::boolean,
                false
            );
        $$
        """
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION auditcore.current_platform_super_admin() TO {_RUNTIME_ROLE}"
    )
    op.execute(
        """
        CREATE POLICY superadmin_project_directory
        ON auditcore.projects
        FOR SELECT
        USING (auditcore.current_platform_super_admin())
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS superadmin_project_directory ON auditcore.projects")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION auditcore.current_platform_super_admin() FROM {_RUNTIME_ROLE}"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.current_platform_super_admin()")
