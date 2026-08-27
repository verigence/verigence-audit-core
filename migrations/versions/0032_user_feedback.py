"""Persist tester feedback and optional screenshots.

Revision ID: 0032_user_feedback
Revises: 0031_uc03_generic_review_fields
Create Date: 2026-08-27
"""
from alembic import op

revision = "0032_user_feedback"
down_revision = "0031_uc03_generic_review_fields"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_MAX_SCREENSHOT_BYTES = 1024 * 1024


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE auditcore.user_feedback (
            feedback_id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                  varchar(128) NOT NULL,
            project_name_snapshot      varchar(240) NOT NULL,
            submitted_by_user_id       varchar(160) NOT NULL,
            submitted_by_display_name  varchar(160),
            submitted_by_role          varchar(32) NOT NULL
                                       CHECK (submitted_by_role IN ('PC', 'TL', 'PM')),
            feedback_text              text NOT NULL
                                       CHECK (char_length(btrim(feedback_text)) BETWEEN 1 AND 4000),
            page_path                  varchar(1024),
            screenshot_file_name       varchar(255),
            screenshot_content_type    varchar(80),
            screenshot_data            bytea,
            created_at_utc             timestamptz NOT NULL DEFAULT now(),
            CHECK (
                (screenshot_data IS NULL
                 AND screenshot_file_name IS NULL
                 AND screenshot_content_type IS NULL)
                OR
                (screenshot_data IS NOT NULL
                 AND screenshot_content_type IN ('image/png', 'image/jpeg', 'image/webp')
                 AND octet_length(screenshot_data) < {_MAX_SCREENSHOT_BYTES})
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_user_feedback_created_at ON auditcore.user_feedback (created_at_utc DESC)"
    )
    op.execute(
        "CREATE INDEX ix_user_feedback_tenant_created ON auditcore.user_feedback "
        "(tenant_id, created_at_utc DESC)"
    )
    op.execute("ALTER TABLE auditcore.user_feedback ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditcore.user_feedback FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_feedback_insert
        ON auditcore.user_feedback
        FOR INSERT
        WITH CHECK (tenant_id = auditcore.current_tenant_id())
        """
    )
    op.execute(
        """
        CREATE POLICY superadmin_feedback_select
        ON auditcore.user_feedback
        FOR SELECT
        USING (auditcore.current_platform_super_admin())
        """
    )
    op.execute(f"GRANT SELECT, INSERT ON auditcore.user_feedback TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON auditcore.user_feedback FROM {_RUNTIME_ROLE}")
    op.execute("DROP TABLE IF EXISTS auditcore.user_feedback")
