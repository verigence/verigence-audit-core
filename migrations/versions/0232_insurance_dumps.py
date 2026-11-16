"""0232_insurance_dumps -- the dealers' monthly insurance dumps and the OD discount grid
(product owner 2026-11-16).

"This is next dump which will come monthly .. insurance dump .. two type of data to be
fetched: as per monthly grid, on insurance, we have to find the records where OD
discount is given more than the agreed discount ... the OD discount matrix has to be
updated for each month, you can give an option to key in the same as well ...
essentially it will have how much OD discount across three different segments for
yearly insurance, and how much for 3+3 insurance, any additional OD discount for
certain models. It could be one or more ... second report ... which deliveries in
the verigence is showing self insurance, but it has been insured from the
dealership."

p2_insurance_dumps        one dealership's policies for a month (the Mahindra insurance
                          "Policy Details Report"); a new upload for the same dealership
                          and month supersedes the last
p2_insurance_dump_rows    one policy each, matched to a Journey of the dealership
p2_od_discount_limits     the month's grid: a SEGMENT row is the agreed maximum OD
                          discount for a vehicle class (PCP / GCV / PCV) and tenure
                          (1Y, 3+3); a MODEL row adds to it for a model (and, if given,
                          a tenure). dealer_id NULL is the grid for every dealership; a
                          dealership's own rows, when it has any for the month, replace it.
"""
from __future__ import annotations

from alembic import op

revision = "0232_insurance_dumps"
down_revision = "0231_dms_outlet_map"
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
        CREATE TABLE IF NOT EXISTS auditcore.p2_insurance_dumps (
            tenant_id         varchar(128) NOT NULL,
            dump_id           uuid         NOT NULL,
            dealer_id         uuid         NOT NULL,
            period_month      date         NOT NULL,
            file_name         varchar(300) NOT NULL,
            row_count         integer      NOT NULL DEFAULT 0,
            duplicate_count   integer      NOT NULL DEFAULT 0,
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
        "CREATE INDEX IF NOT EXISTS ix_p2_insurance_dumps_month "
        "ON auditcore.p2_insurance_dumps (tenant_id, period_month, dealer_id, status)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_insurance_dump_rows (
            tenant_id        varchar(128) NOT NULL,
            dump_id          uuid         NOT NULL,
            row_no           integer      NOT NULL,
            dealer_id        uuid         NOT NULL,
            policy_number    varchar(80)  NOT NULL,
            policy_date      date,
            insurer          varchar(200),
            insured_name     varchar(300),
            mobile           varchar(20),
            vin              varchar(40),
            engine_number    varchar(40),
            model            varchar(200),
            variant          varchar(200),
            segment          varchar(20),
            plan_type        varchar(20),
            od_years         integer,
            od_discount_pct  numeric(6,2),
            idv              numeric(14,2),
            net_od_premium   numeric(14,2),
            misp_code        varchar(120),
            location         varchar(200),
            journey_id       uuid,
            match_basis      varchar(30),
            raw              jsonb        NOT NULL DEFAULT '{}',
            PRIMARY KEY (tenant_id, dump_id, row_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_insurance_dump_rows_journey "
        "ON auditcore.p2_insurance_dump_rows (tenant_id, journey_id) WHERE journey_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_od_discount_limits (
            tenant_id       varchar(128) NOT NULL,
            limit_id        uuid         NOT NULL,
            period_month    date         NOT NULL,
            dealer_id       uuid,
            limit_kind      varchar(10)  NOT NULL CHECK (limit_kind IN ('SEGMENT', 'MODEL')),
            segment         varchar(20),
            tenure          varchar(10)  CHECK (tenure IS NULL OR tenure IN ('1Y', '3+3')),
            model           varchar(200),
            pct             numeric(6,2) NOT NULL CHECK (pct >= 0 AND pct <= 100),
            updated_by      varchar(200),
            updated_at_utc  timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, limit_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_od_discount_limits_month "
        "ON auditcore.p2_od_discount_limits (tenant_id, period_month)"
    )
    for table in ("p2_insurance_dumps", "p2_insurance_dump_rows", "p2_od_discount_limits"):
        _rls(table)
    # A month's grid is replaced as a whole when it is saved again.
    op.execute(f"GRANT DELETE ON auditcore.p2_od_discount_limits TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    for table in ("p2_od_discount_limits", "p2_insurance_dump_rows", "p2_insurance_dumps"):
        op.execute(f"DROP TABLE IF EXISTS auditcore.{table}")
