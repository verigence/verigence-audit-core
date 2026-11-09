"""0205_sevasutra_instances -- a SevaSutra instance for a service-audit Project (product owner, 2026-11-09).

"Add a provision in Project Administration for service audit projects to create a new instance ...
trigger a GitHub action, create a new instance and give the option to add the endpoint."

``sevasutra_instances`` holds, per service-audit Project, the SevaSutra instance the Super Admin asked for:
the dealership's name and its address (``<subdomain>.sevasutra.jbrconsulting.in``). SevaSutra reads it
from the Project feed, creates the dealership, and its deployment workflow (a GitHub Action) puts the
address live; SevaSutra then reports back what it holds (status, dealer code, the live address, when it
last read the Project), which Project Administration shows.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0205_sevasutra_instances"
down_revision = "0204_activity_reading_window"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.sevasutra_instances (
            tenant_id             varchar(128) NOT NULL PRIMARY KEY REFERENCES auditcore.projects(tenant_id),
            dealership_name       varchar(240) NOT NULL,
            subdomain             varchar(40)  CHECK (subdomain ~ '^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$'),
            status                varchar(20)  NOT NULL DEFAULT 'REQUESTED'
                                  CHECK (status IN ('REQUESTED', 'CREATED', 'LIVE', 'FAILED')),
            status_message        varchar(500),
            dealer_code           varchar(60),
            live_hostname         varchar(200),
            requested_by_actor_id varchar(160),
            requested_at_utc      timestamptz  NOT NULL DEFAULT now(),
            reported_at_utc       timestamptz,
            updated_at_utc        timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    # One address per instance across every Project.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_sevasutra_instances_subdomain "
        "ON auditcore.sevasutra_instances (subdomain) WHERE subdomain IS NOT NULL"
    )
    op.execute("ALTER TABLE auditcore.sevasutra_instances ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditcore.sevasutra_instances FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_sevasutra_instances ON auditcore.sevasutra_instances")
    op.execute(
        "CREATE POLICY tenant_isolation_sevasutra_instances ON auditcore.sevasutra_instances "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.sevasutra_instances TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.sevasutra_instances")
