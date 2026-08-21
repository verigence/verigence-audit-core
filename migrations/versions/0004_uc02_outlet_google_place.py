from alembic import op

revision = "0004_uc02_outlet_google_place"
down_revision = "0003_uc02_project_configuring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.dealer_outlets ADD COLUMN google_place_id text"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.dealer_outlets DROP COLUMN google_place_id"
    )
