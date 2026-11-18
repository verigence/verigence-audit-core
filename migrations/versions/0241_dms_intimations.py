"""0241_dms_intimations -- the PCs' intimation marks on the DMS dumps, the generated DMS
reports and the PC day status from myHR (product owner 2026-11-18).

"we should discontinue asking those queries, and build the entire process full
deterministic ... Reviewer or any will upload the booking dump .. message should pop on
every PC ... PC can search and see the booking details and can claim that booking is
intimated ... give a button to generate the report ... this report should move to the
reports section and it can be generated only by Reviewer and above."

- p2_dms_intimations  one per dump row a PC says the dealership intimated to them. Keyed by
                      the row's identity (OTF / booking number for a booking; chassis /
                      invoice for a delivery), so the mark carries to every later dump of
                      the dealership that holds the same row. A withdrawn mark is kept.
- p2_dms_reports      every report generated: the day it is for, who generated it, and the
                      report itself as generated (figures, statements, rows), so a report
                      once generated reads the same later.
- p2_tenant_alerts    gains DMS_DUMP_UPLOADED (the alert to the PCs on an upload).
- hr_pc_day_status    a view of myHR's days (check-in, approved leave) per person, so Audit
                      Core can name the outlets whose PC was not available. Only the person,
                      day and status are exposed; where myHR is not in the database (tests)
                      the view is empty.

Tenant row security as every Audit Core table; the runtime role may read, add and update,
never delete.
"""
from __future__ import annotations

from alembic import op

revision = "0241_dms_intimations"
down_revision = "0240_dms_out_of_period"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLES = ("p2_dms_intimations", "p2_dms_reports")
_KINDS_BEFORE = ("SAME_FILE_UPLOADED", "DUPLICATE_ACROSS_OUTLETS", "DUPLICATE_SAME_DEALERSHIP",
                 "MY_DOCUMENTS_ATTENTION")
_KINDS = (*_KINDS_BEFORE, "DMS_DUMP_UPLOADED")


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE auditcore.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE auditcore.{table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON auditcore.{table}")
    op.execute(
        f"CREATE POLICY tenant_isolation_{table} ON auditcore.{table} "
        "USING (tenant_id = auditcore.current_tenant_id()) "
        "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{table} TO {_RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE ON auditcore.{table} FROM {_RUNTIME_ROLE}")


def _alert_kinds(kinds: tuple[str, ...]) -> None:
    op.execute("ALTER TABLE auditcore.p2_tenant_alerts DROP CONSTRAINT IF EXISTS p2_tenant_alerts_alert_kind_check")
    listed = ", ".join(f"'{k}'" for k in kinds)
    op.execute("ALTER TABLE auditcore.p2_tenant_alerts ADD CONSTRAINT p2_tenant_alerts_alert_kind_check "
               f"CHECK (alert_kind IN ({listed}))")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_dms_intimations (
            tenant_id           varchar(128) NOT NULL,
            intimation_id       uuid         NOT NULL,
            dealer_id           uuid         NOT NULL,
            dump_kind           varchar(10)  NOT NULL CHECK (dump_kind IN ('BOOKING', 'DELIVERY')),
            row_key             varchar(400) NOT NULL,
            dump_id             uuid         NOT NULL,
            row_no              integer      NOT NULL,
            customer_name       varchar(300),
            event_date          date,
            outlet_label        varchar(300),
            claimed_by          varchar(200) NOT NULL,
            claimed_by_name     varchar(300),
            claimed_at_utc      timestamptz  NOT NULL DEFAULT now(),
            withdrawn_at_utc    timestamptz,
            withdrawn_by        varchar(200),
            PRIMARY KEY (tenant_id, intimation_id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_p2_dms_intimations_live ON auditcore.p2_dms_intimations "
        "(tenant_id, dealer_id, dump_kind, row_key) WHERE withdrawn_at_utc IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_dms_intimations_by ON auditcore.p2_dms_intimations "
        "(tenant_id, claimed_by, claimed_at_utc)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_dms_reports (
            tenant_id           varchar(128) NOT NULL,
            report_id           uuid         NOT NULL,
            report_date         date         NOT NULL,
            period_month        date         NOT NULL,
            dealer_ids          text[]       NOT NULL DEFAULT '{}',
            generated_by        varchar(200) NOT NULL,
            generated_by_name   varchar(300),
            generated_at_utc    timestamptz  NOT NULL DEFAULT now(),
            figures             jsonb        NOT NULL DEFAULT '{}'::jsonb,
            report              jsonb        NOT NULL,
            PRIMARY KEY (tenant_id, report_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_dms_reports_day ON auditcore.p2_dms_reports "
        "(tenant_id, report_date, generated_at_utc)"
    )
    for table in _TABLES:
        _secure(table)
    _alert_kinds(_KINDS)
    # myHR's days per person. A view runs as its owner, so the runtime role reads these three
    # columns and nothing else of myHR.
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('hr.attendance_day') IS NOT NULL AND to_regclass('hr.employee') IS NOT NULL
             AND to_regclass('hr.leave_request') IS NOT NULL THEN
            EXECUTE $v$
              CREATE OR REPLACE VIEW auditcore.hr_pc_day_status AS
              SELECT CAST(e.security_user_id AS text) AS security_user_id, a.work_date AS day,
                     CAST('PRESENT' AS varchar(10)) AS status
              FROM hr.attendance_day a JOIN hr.employee e ON e.employee_id = a.employee_id
              WHERE a.check_in_at IS NOT NULL AND e.security_user_id IS NOT NULL
              UNION ALL
              SELECT CAST(e.security_user_id AS text), CAST(g AS date), CAST('LEAVE' AS varchar(10))
              FROM hr.leave_request l JOIN hr.employee e ON e.employee_id = l.employee_id,
                   generate_series(l.from_date, l.to_date, interval '1 day') g
              WHERE l.status = 'APPROVED' AND e.security_user_id IS NOT NULL
            $v$;
          ELSE
            EXECUTE $v$
              CREATE OR REPLACE VIEW auditcore.hr_pc_day_status AS
              SELECT CAST(NULL AS text) AS security_user_id, CAST(NULL AS date) AS day,
                     CAST(NULL AS varchar(10)) AS status
              WHERE false
            $v$;
          END IF;
        END $$;
        """
    )
    op.execute(f"GRANT SELECT ON auditcore.hr_pc_day_status TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS auditcore.hr_pc_day_status")
    op.execute("DELETE FROM auditcore.p2_tenant_alerts WHERE alert_kind = 'DMS_DUMP_UPLOADED'")
    _alert_kinds(_KINDS_BEFORE)
    op.execute("DROP TABLE IF EXISTS auditcore.p2_dms_reports")
    op.execute("DROP TABLE IF EXISTS auditcore.p2_dms_intimations")
