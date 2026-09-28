"""0131_onboarding_workbook — Excel-templated Project / Dealer / Outlet onboarding.

- ``projects.business_code``: the Project Code people use (e.g. JBR-01),
  unique across Projects regardless of case. ``project_code`` keeps the
  Security tenant code it has always held (analytics reports are keyed on
  it), so nothing that reads it changes.
- ``onboarding_imports``: one row per uploaded onboarding workbook -- the
  validated plan shown in the preview and, once confirmed, the result.
  Platform-level (a workbook may span Projects), written only by SuperAdmin
  endpoints; no tenant RLS.
"""
from __future__ import annotations

from alembic import op

revision = "0131_onboarding_workbook"
down_revision = "0130_uc03_p2_live_notify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auditcore.projects ADD COLUMN IF NOT EXISTS business_code varchar(40)")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_business_code
          ON auditcore.projects (upper(business_code))
          WHERE business_code IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.onboarding_imports (
          import_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          created_by_actor_id varchar(128) NOT NULL,
          original_filename varchar(260) NOT NULL,
          file_sha256 char(64) NOT NULL,
          import_status varchar(24) NOT NULL
            CHECK (import_status IN ('PREVIEW_READY','VALIDATION_FAILED','APPLYING','APPLIED','APPLIED_WITH_ERRORS')),
          plan jsonb NOT NULL,
          result jsonb,
          created_at_utc timestamptz NOT NULL DEFAULT now(),
          applied_at_utc timestamptz
        );
        CREATE INDEX IF NOT EXISTS ix_onboarding_imports_recent
          ON auditcore.onboarding_imports (created_at_utc DESC);
        GRANT SELECT, INSERT, UPDATE ON auditcore.onboarding_imports TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.onboarding_imports")
    op.execute("DROP INDEX IF EXISTS auditcore.ux_projects_business_code")
    op.execute("ALTER TABLE auditcore.projects DROP COLUMN IF EXISTS business_code")
