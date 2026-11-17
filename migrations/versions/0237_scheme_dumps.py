"""0237_scheme_dumps -- the dealers' monthly extended warranty (EW) and RSA dumps and
what Verigence shows for the Journeys they name (product owner, 2026-11-17).

"upload use case, now it is Extended warranty and RSA ... this is monthly upload, what we
need to do is compare ... the excel sheet and found matching record in verigence and then
need to check if they have bought EW and RSA ... report which all have bought and in sync,
second is customer who didn't bought as per verigence, and third is customers who are not
in this list."

p2_scheme_dumps           one dealership's EW or RSA register for a month; ``locations``
                          are the Location names the file carries (empty: the whole
                          dealership -- Aditya uploads Jajpur apart from the rest); a new
                          upload for the same dealership, type, month and Location
                          supersedes the last
p2_scheme_dump_rows       one scheme registration each, matched to a Journey of the
                          dealership (chassis, DMS customer id, registration number)
p2_scheme_journey_checks  per Journey, what Verigence shows: the customer was charged for
                          an extended warranty / an RSA (the one "sold" rule,
                          uc03_p2_stage.condition_reasons) and whether its NDC / ledger is
                          on file; decided on upload, on "Check again" and nightly

Additive; idempotent. Tenant RLS; the runtime role cannot DELETE.
"""
from __future__ import annotations

from alembic import op

revision = "0237_scheme_dumps"
down_revision = "0236_insurance_policy_type"
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
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{table} TO {_RUNTIME_ROLE}")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_scheme_dumps (
            tenant_id         varchar(128) NOT NULL,
            dump_id           uuid         NOT NULL,
            dump_kind         varchar(10)  NOT NULL CHECK (dump_kind IN ('EW', 'RSA')),
            dealer_id         uuid         NOT NULL,
            period_month      date         NOT NULL,
            file_name         varchar(300) NOT NULL,
            file_format       varchar(20),
            locations         text[]       NOT NULL DEFAULT '{}',
            row_count         integer      NOT NULL DEFAULT 0,
            duplicate_count   integer      NOT NULL DEFAULT 0,
            sales_count       integer      NOT NULL DEFAULT 0,
            service_count     integer      NOT NULL DEFAULT 0,
            cancelled_count   integer      NOT NULL DEFAULT 0,
            matched_count     integer      NOT NULL DEFAULT 0,
            status            varchar(20)  NOT NULL DEFAULT 'ACTIVE',
            uploaded_by       varchar(200) NOT NULL,
            uploaded_by_name  varchar(300),
            uploaded_at_utc   timestamptz  NOT NULL DEFAULT now(),
            matched_at_utc    timestamptz,
            PRIMARY KEY (tenant_id, dump_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_scheme_dumps_month "
        "ON auditcore.p2_scheme_dumps (tenant_id, period_month, dump_kind, dealer_id, status)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_scheme_dump_rows (
            tenant_id            varchar(128) NOT NULL,
            dump_id              uuid         NOT NULL,
            row_no               integer      NOT NULL,
            dealer_id            uuid         NOT NULL,
            dump_kind            varchar(10)  NOT NULL,
            scheme_number        varchar(80),
            scheme_date          date,
            scheme               varchar(300),
            customer_code        varchar(40),
            gstin                varchar(20),
            customer_name        varchar(300),
            mobile               varchar(20),
            chassis              varchar(40),
            registration_number  varchar(40),
            model                varchar(200),
            sold_by              varchar(40),
            channel              varchar(10)  NOT NULL DEFAULT 'UNKNOWN',
            category             varchar(80),
            km                   integer,
            employee             varchar(200),
            scheme_status        varchar(80),
            cancelled            boolean      NOT NULL DEFAULT false,
            cancelled_on         date,
            scheme_amount        numeric(14,2),
            tax_amount           numeric(14,2),
            location             varchar(200),
            journey_id           uuid,
            match_basis          varchar(30),
            match_note           varchar(40),
            raw                  jsonb        NOT NULL DEFAULT '{}',
            PRIMARY KEY (tenant_id, dump_id, row_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_scheme_dump_rows_journey "
        "ON auditcore.p2_scheme_dump_rows (tenant_id, journey_id) WHERE journey_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_scheme_journey_checks (
            tenant_id        varchar(128) NOT NULL,
            journey_id       uuid         NOT NULL,
            dealer_id        uuid         NOT NULL,
            ew_sold          boolean      NOT NULL DEFAULT false,
            ew_reason        text,
            rsa_sold         boolean      NOT NULL DEFAULT false,
            rsa_reason       text,
            papers_on_file   boolean      NOT NULL DEFAULT false,
            checked_at_utc   timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, journey_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_scheme_journey_checks_dealer "
        "ON auditcore.p2_scheme_journey_checks (tenant_id, dealer_id)"
    )
    for table in ("p2_scheme_dumps", "p2_scheme_dump_rows", "p2_scheme_journey_checks"):
        _rls(table)


def downgrade() -> None:
    for table in ("p2_scheme_journey_checks", "p2_scheme_dump_rows", "p2_scheme_dumps"):
        op.execute(f"DROP TABLE IF EXISTS auditcore.{table}")
