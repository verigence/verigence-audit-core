"""0223_dms_dumps -- the dealers' DMS booking and delivery dumps (product owner 2026-11-13).

A Team Lead or Reviewer uploads each dealership's DMS booking report and delivery report
(month to date, uploaded daily). Every row is matched against the Journeys on file; the
rows not found are the missing bookings / missing deliveries, reported per upload and
downloadable as Excel. A Journey a dump row matched is marked synced.

- p2_dms_dumps         one per file uploaded: dealership, booking or delivery, the day it
                       is for, who uploaded it, its counts. Uploading the same dealership,
                       kind and day again supersedes the earlier one (kept, never deleted).
- p2_dms_dump_rows     every row of the file, as read (raw kept), with the Journey it
                       matched and on what (booking number, chassis, mobile, name + model).
- p2_journey_dms_sync  the two marks on a Journey: when a DMS booking dump and when a DMS
                       delivery dump first matched it (and which upload). Kept beside the
                       Journey so marking one never touches the Journey row itself.

Tenant row security as every Audit Core table; the runtime role may read, add and update,
never delete.
"""
from __future__ import annotations

from alembic import op

revision = "0223_dms_dumps"
down_revision = "0222_performance_settings"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLES = ("p2_dms_dumps", "p2_dms_dump_rows", "p2_journey_dms_sync")


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


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_dms_dumps (
            tenant_id           varchar(128) NOT NULL,
            dump_id             uuid         NOT NULL,
            dump_kind           varchar(10)  NOT NULL CHECK (dump_kind IN ('BOOKING', 'DELIVERY')),
            dealer_id           uuid         NOT NULL,
            business_date       date         NOT NULL,
            period_month        date         NOT NULL,
            file_name           varchar(300) NOT NULL,
            file_format         varchar(40)  NOT NULL,
            sheet_name          varchar(120),
            status              varchar(12)  NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUPERSEDED')),
            row_count           integer      NOT NULL DEFAULT 0,
            matched_count       integer      NOT NULL DEFAULT 0,
            missing_count       integer      NOT NULL DEFAULT 0,
            not_in_tool_count   integer      NOT NULL DEFAULT 0,
            cancelled_count     integer      NOT NULL DEFAULT 0,
            uploaded_by         varchar(200) NOT NULL,
            uploaded_by_name    varchar(300),
            uploaded_at_utc     timestamptz  NOT NULL DEFAULT now(),
            matched_at_utc      timestamptz,
            PRIMARY KEY (tenant_id, dump_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_dms_dumps_month ON auditcore.p2_dms_dumps "
        "(tenant_id, period_month, dealer_id, dump_kind, business_date)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_dms_dump_rows (
            tenant_id           varchar(128) NOT NULL,
            dump_id             uuid         NOT NULL,
            row_no              integer      NOT NULL,
            dump_kind           varchar(10)  NOT NULL,
            dealer_id           uuid         NOT NULL,
            outlet_label        varchar(200),
            outlet_id           uuid,
            event_date          date,
            booking_number      varchar(60),
            dms_booking_number  varchar(60),
            customer_name       varchar(300),
            mobile              varchar(20),
            chassis             varchar(40),
            invoice_number      varchar(60),
            registration_number varchar(40),
            model               varchar(200),
            variant             varchar(200),
            stage               varchar(60),
            consultant          varchar(200),
            raw                 jsonb        NOT NULL DEFAULT '{}'::jsonb,
            journey_id          uuid,
            match_basis         varchar(20),
            outcome             varchar(16)  NOT NULL DEFAULT 'MISSING'
                                  CHECK (outcome IN ('MATCHED', 'NOT_IN_TOOL', 'MISSING', 'CANCELLED')),
            PRIMARY KEY (tenant_id, dump_id, row_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_dms_dump_rows_journey ON auditcore.p2_dms_dump_rows "
        "(tenant_id, journey_id) WHERE journey_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_journey_dms_sync (
            tenant_id                 varchar(128) NOT NULL,
            journey_id                uuid         NOT NULL,
            dms_booking_synced_at     timestamptz,
            dms_booking_dump_id       uuid,
            dms_delivery_synced_at    timestamptz,
            dms_delivery_dump_id      uuid,
            PRIMARY KEY (tenant_id, journey_id)
        )
        """
    )
    for table in _TABLES:
        _secure(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS auditcore.{table}")
