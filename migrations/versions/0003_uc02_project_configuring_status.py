from alembic import op

revision = "0003_uc02_project_configuring"
down_revision = "0002_runtime_role_rls"
branch_labels = None
depends_on = None


_PROJECT_STATUS_CONSTRAINT = "projects_project_status_check"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE auditcore.projects DROP CONSTRAINT IF EXISTS {_PROJECT_STATUS_CONSTRAINT}"
    )
    op.execute(
        f"""
        ALTER TABLE auditcore.projects
        ADD CONSTRAINT {_PROJECT_STATUS_CONSTRAINT}
        CHECK (project_status IN ('DRAFT','CONFIGURING','ACTIVE','INACTIVE','CLOSED'))
        """
    )


def downgrade() -> None:
    # CONFIGURING did not exist before UC02. Map any pre-activation rows back to the
    # closest pre-UC02 setup state so the previous constraint can be restored.
    op.execute(
        "UPDATE auditcore.projects SET project_status='DRAFT' WHERE project_status='CONFIGURING'"
    )
    op.execute(
        f"ALTER TABLE auditcore.projects DROP CONSTRAINT IF EXISTS {_PROJECT_STATUS_CONSTRAINT}"
    )
    op.execute(
        f"""
        ALTER TABLE auditcore.projects
        ADD CONSTRAINT {_PROJECT_STATUS_CONSTRAINT}
        CHECK (project_status IN ('DRAFT','ACTIVE','INACTIVE','CLOSED'))
        """
    )
