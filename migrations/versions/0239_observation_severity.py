"""0239_observation_severity -- each journey's observation severity (product owner, 2026-11-18).

"if NDC doesnt show fast tag and invoice has ... mark this as transaction with
observation level as high ... lets add column observation severity high low
medium, and lets give entire row a color, and add filter in search... severity
can be changed by Reviewer, TL, PM".

p2_observation_severity   one row per journey: the severity the system sets
                          (with the reasons it set it for) and the one a
                          Reviewer / Team Lead / Project Manager set over it
                          (who, when, why). The journey's severity is the
                          person's when set, else the system's.

Additive; idempotent. Tenant RLS; the runtime role cannot DELETE.
"""
from __future__ import annotations

from alembic import op

revision = "0239_observation_severity"
down_revision = "0238_verified_bookings"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLE = "p2_observation_severity"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS auditcore.{_TABLE} (
            tenant_id            varchar(128) NOT NULL,
            journey_id           uuid         NOT NULL,
            system_severity      varchar(10)  CHECK (system_severity IN ('HIGH', 'MEDIUM', 'LOW')),
            system_reasons       jsonb        NOT NULL DEFAULT '[]'::jsonb,
            system_at_utc        timestamptz,
            override_severity    varchar(10)  CHECK (override_severity IN ('HIGH', 'MEDIUM', 'LOW')),
            override_reason      text,
            override_by          varchar(200),
            override_by_name     varchar(200),
            override_role        varchar(40),
            override_at_utc      timestamptz,
            PRIMARY KEY (tenant_id, journey_id)
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_severity ON auditcore.{_TABLE} "
        "(tenant_id, (COALESCE(override_severity, system_severity)))"
    )
    op.execute(f"ALTER TABLE auditcore.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{_TABLE} ON auditcore.{_TABLE}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{_TABLE} ON auditcore.{_TABLE} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{_TABLE} TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS auditcore.{_TABLE}")
