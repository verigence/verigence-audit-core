from alembic import op

revision = "0005_uc02_admin_row_delete"
down_revision = "0004_uc02_outlet_google_place"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # UC02 Phase 1 explicitly permits SuperAdmin hard delete for Dealer/Outlet
    # administration only. Keep DELETE denied on the rest of the business schema.
    op.execute(
        f"GRANT DELETE ON auditcore.dealers, auditcore.dealer_outlets TO {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        f"REVOKE DELETE ON auditcore.dealers, auditcore.dealer_outlets FROM {_RUNTIME_ROLE}"
    )
