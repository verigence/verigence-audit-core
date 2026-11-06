"""0197_p2_journey_exports -- journey export to Excel for Team Leads and above (2026-11-06).

Product owner: "give an option to TL and above to download all the journey as
excel for given date range or dealerwise or outlet wise or all". An export is a
job: requested by a person, built by the P2 worker one journey at a time, its
workbook stored in the document bucket, downloaded through a short-lived link.

``outlet_ids``: the outlets the requester supervised when they asked (TL, PM or
Executive on them), so the worker builds exactly what they may see. Tenant-isolated like every P2 table. Additive.
"""
from __future__ import annotations

from alembic import op

revision = "0197_p2_journey_exports"
down_revision = "0196_queue_quality_confirmed"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_journey_exports"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id              varchar(128) NOT NULL,
            export_id              uuid NOT NULL,
            requested_by_actor_id  varchar(256) NOT NULL,
            requested_by_role      varchar(32) NOT NULL,
            filters                jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            outlet_ids             uuid[],
            names                  jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            file_name              varchar(300) NOT NULL,
            status                 varchar(16) NOT NULL DEFAULT 'PENDING'
                                   CHECK (status IN ('PENDING', 'RUNNING', 'READY', 'FAILED')),
            total                  integer,
            done                   integer NOT NULL DEFAULT 0,
            row_count              integer,
            size_bytes             bigint,
            object_key             varchar(1024),
            error                  text,
            worker_id              varchar(200),
            requested_at_utc       timestamptz NOT NULL DEFAULT now(),
            started_at_utc         timestamptz,
            heartbeat_at_utc       timestamptz,
            finished_at_utc        timestamptz,
            PRIMARY KEY (tenant_id, export_id)
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_open ON auditcore.{_TABLE} (tenant_id, requested_at_utc) "
        "WHERE status IN ('PENDING', 'RUNNING')"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_requester "
        f"ON auditcore.{_TABLE} (tenant_id, requested_by_actor_id, requested_at_utc DESC)"
    )
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_RUNTIME_ROLE}') THEN "
        f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
