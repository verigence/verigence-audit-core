"""0195_project_audit_type -- what a Project audits: sales or service (product owner, 2026-11-05).

"Shree Group - Service Audit ... it's a service audit, it doesn't need a sales
price book." Until now every Project was treated as a sales (booking and
delivery) audit: the Mahindra price-book, scheme and grid jobs ran on it, and
its readiness asked for the sales price, product and discount masters.

``projects.audit_type``: SALES (every existing Project, the default) or SERVICE.
Sales masters and the jobs that load them apply to SALES Projects only.
"Shree Group - Service Audit" (created 5 Nov 2026) is a SERVICE Project.
Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0195_project_audit_type"
down_revision = "0194_dealguard_location_checks"
branch_labels = None
depends_on = None

_SHREE_SERVICE_AUDIT = "6f5c72c9-0775-41bd-9ba4-f68803bf3f7d"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE auditcore.projects ADD COLUMN IF NOT EXISTS audit_type varchar(20) NOT NULL DEFAULT 'SALES'"
    )
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE auditcore.projects ADD CONSTRAINT projects_audit_type_check
                CHECK (audit_type IN ('SALES', 'SERVICE'));
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """
    )
    op.execute(
        f"UPDATE auditcore.projects SET audit_type = 'SERVICE' WHERE tenant_id = '{_SHREE_SERVICE_AUDIT}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE auditcore.projects DROP CONSTRAINT IF EXISTS projects_audit_type_check")
    op.execute("ALTER TABLE auditcore.projects DROP COLUMN IF EXISTS audit_type")
