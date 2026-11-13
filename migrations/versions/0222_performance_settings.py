"""0222_performance_settings -- what the Performance view shows, set by a SuperAdmin.

Product owner, 13 Nov 2026: "can we make the parameters to be displayed configurable
under Superadmin control, like utilization and all".

platform_ops.performance_settings: one row per item a SuperAdmin has switched (a
measure, the duties, the comparison with peers). An item with no row is shown.
Platform-level, like the Jobs page: one setting for every Project.
"""
from __future__ import annotations

from alembic import op

revision = "0222_performance_settings"
down_revision = "0221_lookup_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS platform_ops;
        GRANT USAGE ON SCHEMA platform_ops TO audit_core_runtime;

        CREATE TABLE IF NOT EXISTS platform_ops.performance_settings (
          item_key varchar(60) PRIMARY KEY,
          shown boolean NOT NULL,
          updated_by varchar(160),
          updated_at_utc timestamptz NOT NULL DEFAULT now()
        );
        GRANT SELECT, INSERT, UPDATE ON platform_ops.performance_settings TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS platform_ops.performance_settings")
