"""0246_task_switches -- a kind of task switched off or on per Project (2026-11-19).

Product owner 2026-11-19: "is there a way i can see the list of the tasks being produced
and tick them off, because some of the tasks are not relevant currently, but it may be
required in future ... at the project level and it should be under project administration
only"; switched off, the check stops and "it shouldnt be visible for the existing journey as
well" (uc03_task_switches).

* ``p2_task_switches``: one row per Project and task code (the check or producer's code);
  no row means on.
* ``p2_task_switch_events``: every change, who, when and why.
* The two checks already switched off per Project move here, off where they were off:
  "Accessories fitted are billed" (ACCESSORIES_FITTED_UNCONFIRMED, 0213) and "Delivery not
  completed in time" (DELIVERY_NOT_COMPLETED_IN_TIME, 0244), for every Project with a
  booking or a rule configuration. tenant_rule_config's two columns are no longer read.

Additive; idempotent.
"""
from __future__ import annotations

from alembic import op

revision = "0246_task_switches"
down_revision = "0245_dms_delivery_scope"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE auditcore.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON auditcore.{table}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{table} ON auditcore.{table} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_task_switches (
            tenant_id        varchar(128) NOT NULL,
            source_code      varchar(120) NOT NULL,
            enabled          boolean      NOT NULL,
            reason           text,
            changed_by       varchar(200),
            changed_by_name  varchar(200),
            changed_at_utc   timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, source_code)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_task_switch_events (
            event_id         bigserial    PRIMARY KEY,
            tenant_id        varchar(128) NOT NULL,
            source_code      varchar(120) NOT NULL,
            enabled          boolean      NOT NULL,
            reason           text,
            changed_by       varchar(200),
            changed_by_name  varchar(200),
            changed_at_utc   timestamptz  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_p2_task_switch_events_code "
               "ON auditcore.p2_task_switch_events (tenant_id, source_code, changed_at_utc DESC)")
    # The two checks switched off before this list existed (seeded before RLS is forced).
    op.execute(
        """
        INSERT INTO auditcore.p2_task_switches (tenant_id, source_code, enabled, reason, changed_by_name)
        SELECT p.tenant_id, s.code, false, s.reason, 'Product owner'
        FROM (SELECT tenant_id FROM auditcore.journeys
              UNION SELECT tenant_id FROM auditcore.tenant_rule_config) p
        CROSS JOIN (VALUES
            ('ACCESSORIES_FITTED_UNCONFIRMED', 'accessories_fitted_check_enabled',
             'Not relevant for now; may be introduced later (product owner, 12 Nov 2026).'),
            ('DELIVERY_NOT_COMPLETED_IN_TIME', 'delivery_in_time_check_enabled',
             'Not relevant for now; may be introduced later (product owner, 19 Nov 2026).')
        ) AS s(code, flag, reason)
        LEFT JOIN auditcore.tenant_rule_config c ON c.tenant_id = p.tenant_id
        WHERE NOT COALESCE(CASE s.flag WHEN 'accessories_fitted_check_enabled' THEN c.accessories_fitted_check_enabled
                                       ELSE c.delivery_in_time_check_enabled END, false)
        ON CONFLICT (tenant_id, source_code) DO NOTHING
        """
    )
    for table in ("p2_task_switches", "p2_task_switch_events"):
        _rls(table)
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.p2_task_switches TO {_RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON auditcore.p2_task_switch_events TO {_RUNTIME_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE auditcore.p2_task_switch_events_event_id_seq TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auditcore.p2_task_switch_events")
    op.execute("DROP TABLE IF EXISTS auditcore.p2_task_switches")
