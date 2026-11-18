"""0242_daily_duties -- the PC's daily duties (product owner, 2026-11-18).

"each PC is supposed to do 5-8 tasks in a day, it may change ... let PC know which
tasks are pending ... at day end they have to complete all tasks, or if not completed
need to give the reason. If something is not completed, it has to be clubbed next day
... TL should be able to see the progress of each" -- and "dont want to make it tightly
coupled", "we should be able to switch on/off this any time", "a simple checkbox type".

Its own tables (docs/DAILY_DUTIES_DESIGN_v1.0.md); nothing existing is altered.

p2_duty_settings    one row per Project: the switch (off by default), the day cut-off,
                    the carry days, the PC's working weekdays
p2_duties           the duty list an Executive / SuperAdmin keeps
p2_duty_items       one row per duty, outlet and day it is for; a copy of the duty as it
                    was that day, the tick, the numbers entered
p2_duty_reasons     why an item was not done on a day, per person (and, for "not received
                    today", whether the PC told the relevant WhatsApp group)
p2_duty_day_closes  a PC's day close (at check-out)
p2_duty_events      who did what, when

Additive; idempotent. Tenant RLS; the runtime role cannot DELETE.
"""
from __future__ import annotations

from alembic import op

revision = "0242_daily_duties"
down_revision = "0241_dms_intimations"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"
_TABLES = ("p2_duty_settings", "p2_duties", "p2_duty_items", "p2_duty_reasons", "p2_duty_day_closes",
           "p2_duty_events")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_duty_settings (
            tenant_id            varchar(128) NOT NULL,
            enabled              boolean      NOT NULL DEFAULT false,
            enabled_from         date,
            cutoff_time_ist      time         NOT NULL DEFAULT '21:00',
            default_carry_days   smallint     NOT NULL DEFAULT 5 CHECK (default_carry_days BETWEEN 0 AND 30),
            working_weekdays     smallint[]   NOT NULL DEFAULT ARRAY[1,2,3,4,5,6]::smallint[],
            generated_through    date,
            updated_by           varchar(200),
            updated_by_name      varchar(200),
            updated_at_utc       timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_duties (
            tenant_id            varchar(128) NOT NULL,
            duty_id              uuid         NOT NULL DEFAULT gen_random_uuid(),
            standard_code        varchar(40),
            title                varchar(200) NOT NULL,
            description          text         NOT NULL DEFAULT '',
            mandatory            boolean      NOT NULL DEFAULT true,
            weekdays             smallint[]   NOT NULL DEFAULT ARRAY[1,2,3,4,5,6]::smallint[],
            outlet_scope         varchar(12)  NOT NULL DEFAULT 'ALL'
                                   CHECK (outlet_scope IN ('ALL', 'ONSITE', 'SATELLITE', 'SELECTED')),
            outlet_ids           uuid[]       NOT NULL DEFAULT ARRAY[]::uuid[],
            day_offset           smallint     NOT NULL DEFAULT 0 CHECK (day_offset IN (0, -1)),
            close_mode           varchar(12)  NOT NULL DEFAULT 'TICK' CHECK (close_mode IN ('TICK', 'AUTO')),
            checker_code         varchar(40),
            inputs               jsonb        NOT NULL DEFAULT '[]'::jsonb,
            allow_not_received   boolean      NOT NULL DEFAULT false,
            rolling              boolean      NOT NULL DEFAULT false,
            carry_days           smallint     CHECK (carry_days IS NULL OR carry_days BETWEEN 0 AND 30),
            sort_order           integer      NOT NULL DEFAULT 100,
            status               varchar(12)  NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'INACTIVE')),
            effective_from       date         NOT NULL,
            created_by           varchar(200),
            created_by_name      varchar(200),
            created_at_utc       timestamptz  NOT NULL DEFAULT now(),
            updated_by           varchar(200),
            updated_by_name      varchar(200),
            updated_at_utc       timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, duty_id),
            CHECK (close_mode <> 'AUTO' OR checker_code IS NOT NULL)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_p2_duties_standard ON auditcore.p2_duties (tenant_id, standard_code) "
        "WHERE standard_code IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_duty_items (
            tenant_id            varchar(128) NOT NULL,
            item_id              uuid         NOT NULL DEFAULT gen_random_uuid(),
            duty_id              uuid         NOT NULL,
            dealer_id            uuid         NOT NULL,
            outlet_id            uuid         NOT NULL,
            duty_date            date         NOT NULL,
            for_date             date         NOT NULL,
            title                varchar(200) NOT NULL,
            description          text         NOT NULL DEFAULT '',
            mandatory            boolean      NOT NULL,
            close_mode           varchar(12)  NOT NULL CHECK (close_mode IN ('TICK', 'AUTO')),
            checker_code         varchar(40),
            inputs               jsonb        NOT NULL DEFAULT '[]'::jsonb,
            allow_not_received   boolean      NOT NULL DEFAULT false,
            rolling              boolean      NOT NULL DEFAULT false,
            carry_days           smallint     NOT NULL CHECK (carry_days BETWEEN 0 AND 30),
            sort_order           integer      NOT NULL DEFAULT 100,
            status               varchar(12)  NOT NULL DEFAULT 'PENDING'
                                   CHECK (status IN ('PENDING', 'DONE', 'CANCELLED')),
            input_values         jsonb        NOT NULL DEFAULT '{}'::jsonb,
            comment              text,
            done_at_utc          timestamptz,
            done_on              date,
            done_by_actor_id     varchar(200),
            done_by_name         varchar(200),
            done_source          varchar(10)  CHECK (done_source IS NULL OR done_source IN ('PC', 'SYSTEM')),
            cancelled_at_utc     timestamptz,
            cancel_reason        varchar(40),
            created_at_utc       timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, item_id),
            FOREIGN KEY (tenant_id, duty_id) REFERENCES auditcore.p2_duties (tenant_id, duty_id),
            UNIQUE (tenant_id, duty_id, outlet_id, for_date)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_duty_items_outlet_day ON auditcore.p2_duty_items "
        "(tenant_id, outlet_id, duty_date)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_duty_items_open ON auditcore.p2_duty_items (tenant_id, duty_date) "
        "WHERE status = 'PENDING'"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_duty_reasons (
            tenant_id            varchar(128) NOT NULL,
            reason_id            uuid         NOT NULL DEFAULT gen_random_uuid(),
            item_id              uuid         NOT NULL,
            on_date              date         NOT NULL,
            actor_id             varchar(200) NOT NULL,
            actor_name           varchar(200),
            category             varchar(30)  NOT NULL,
            side                 varchar(12)  NOT NULL,
            remark               text,
            handled_by_actor_id  varchar(200),
            handled_by_name      varchar(200),
            informed_group       boolean,
            created_at_utc       timestamptz  NOT NULL DEFAULT now(),
            updated_at_utc       timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, reason_id),
            FOREIGN KEY (tenant_id, item_id) REFERENCES auditcore.p2_duty_items (tenant_id, item_id),
            UNIQUE (tenant_id, item_id, on_date, actor_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_duty_reasons_day ON auditcore.p2_duty_reasons (tenant_id, on_date)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_duty_day_closes (
            tenant_id            varchar(128) NOT NULL,
            actor_id             varchar(200) NOT NULL,
            close_date           date         NOT NULL,
            actor_name           varchar(200),
            closed_at_utc        timestamptz  NOT NULL DEFAULT now(),
            open_mandatory       integer      NOT NULL DEFAULT 0,
            open_optional        integer      NOT NULL DEFAULT 0,
            updated_at_utc       timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, actor_id, close_date)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auditcore.p2_duty_events (
            tenant_id            varchar(128) NOT NULL,
            event_id             uuid         NOT NULL DEFAULT gen_random_uuid(),
            item_id              uuid,
            duty_id              uuid,
            event_type           varchar(40)  NOT NULL,
            actor_id             varchar(200),
            actor_name           varchar(200),
            actor_role           varchar(40),
            details              jsonb        NOT NULL DEFAULT '{}'::jsonb,
            occurred_at_utc      timestamptz  NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, event_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2_duty_events_item ON auditcore.p2_duty_events (tenant_id, item_id)"
    )
    for table in _TABLES:
        op.execute(f"ALTER TABLE auditcore.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE auditcore.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON auditcore.{table}")
        op.execute(
            f"CREATE POLICY tenant_isolation_{table} ON auditcore.{table} "
            "USING (tenant_id = auditcore.current_tenant_id()) "
            "WITH CHECK (tenant_id = auditcore.current_tenant_id())"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON auditcore.{table} TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS auditcore.{table}")
