"""0164_task_manager -- Task Manager, recurring tasks, daily gate pass task and
time on Verigence (product owner, 2026-10-26; docs/TASK_MANAGER_DESIGN_v1.0.md).

Additive for every existing row:

- ``p2_tasks`` gains a scope (``JOURNEY`` for every existing row, ``OUTLET`` or
  ``PROJECT`` for Task Manager work, which has no Journey), the expected time
  to close, a reviewer and its SLA, who completed it and when, the schedule
  occurrence it belongs to, a Missed mark and display names. ``journey_id``
  becomes nullable, guarded by a scope check, and two completion protocols are
  added: SELF_CLOSING (no reviewer) and SYSTEM_VERIFIED (the gate pass task).
- ``p2_task_events.journey_id`` becomes nullable for the same tasks.
- the Journey live-notify trigger on ``p2_tasks`` skips rows without a Journey.
- new ``p2_task_schedules`` (recurring tasks) and ``p2_user_activity_daily``
  (time on Verigence, one row per person, Project, IST day and client).
- the gate-pass mismatch tasks of ``p2_daily_tasks`` are copied into
  ``p2_tasks`` as OUTLET tasks; ``p2_daily_tasks`` is kept, no longer written.
"""
from __future__ import annotations

from alembic import op

revision = "0164_task_manager"
down_revision = "0163_daily_gate_pass"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.p2_tasks
          ADD COLUMN IF NOT EXISTS scope_kind varchar(20) NOT NULL DEFAULT 'JOURNEY',
          ADD COLUMN IF NOT EXISTS dealer_id uuid,
          ADD COLUMN IF NOT EXISTS outlet_id uuid,
          ADD COLUMN IF NOT EXISTS expected_close_minutes integer,
          ADD COLUMN IF NOT EXISTS reviewer_role_code varchar(80),
          ADD COLUMN IF NOT EXISTS reviewer_actor_id varchar(160),
          ADD COLUMN IF NOT EXISTS review_due_at_utc timestamptz,
          ADD COLUMN IF NOT EXISTS action_completed_at_utc timestamptz,
          ADD COLUMN IF NOT EXISTS completed_by_actor_id varchar(160),
          ADD COLUMN IF NOT EXISTS schedule_id uuid,
          ADD COLUMN IF NOT EXISTS occurrence_date date,
          ADD COLUMN IF NOT EXISTS missed_at_utc timestamptz,
          ADD COLUMN IF NOT EXISTS raised_by_display_name varchar(200),
          ADD COLUMN IF NOT EXISTS assigned_display_name varchar(200);

        ALTER TABLE auditcore.p2_tasks ALTER COLUMN journey_id DROP NOT NULL;

        ALTER TABLE auditcore.p2_tasks DROP CONSTRAINT IF EXISTS p2_tasks_scope_kind_check;
        ALTER TABLE auditcore.p2_tasks ADD CONSTRAINT p2_tasks_scope_kind_check CHECK (
          (scope_kind = 'JOURNEY' AND journey_id IS NOT NULL)
          OR (scope_kind = 'OUTLET' AND journey_id IS NULL AND outlet_id IS NOT NULL)
          OR (scope_kind = 'PROJECT' AND journey_id IS NULL)
        );
        ALTER TABLE auditcore.p2_tasks DROP CONSTRAINT IF EXISTS p2_tasks_expected_close_minutes_check;
        ALTER TABLE auditcore.p2_tasks ADD CONSTRAINT p2_tasks_expected_close_minutes_check
          CHECK (expected_close_minutes IS NULL OR expected_close_minutes > 0);

        ALTER TABLE auditcore.p2_tasks DROP CONSTRAINT IF EXISTS p2_tasks_completion_protocol_check;
        ALTER TABLE auditcore.p2_tasks ADD CONSTRAINT p2_tasks_completion_protocol_check
          CHECK (completion_protocol IN (
            'MACHINE_VERIFIED', 'REQUESTER_CONFIRMED', 'SELF_CLOSING', 'SYSTEM_VERIFIED'
          ));

        CREATE INDEX IF NOT EXISTS ix_p2_tasks_task_manager
          ON auditcore.p2_tasks (tenant_id, scope_kind, task_status, due_at_utc)
          WHERE scope_kind <> 'JOURNEY';
        CREATE INDEX IF NOT EXISTS ix_p2_tasks_assigned_actor
          ON auditcore.p2_tasks (tenant_id, assigned_actor_id, task_status);
        CREATE INDEX IF NOT EXISTS ix_p2_tasks_occurrence
          ON auditcore.p2_tasks (tenant_id, schedule_id, occurrence_date)
          WHERE schedule_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_p2_tasks_completed_at
          ON auditcore.p2_tasks (tenant_id, action_completed_at_utc)
          WHERE action_completed_at_utc IS NOT NULL;

        ALTER TABLE auditcore.p2_task_events ALTER COLUMN journey_id DROP NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_p2_task_events_actor_time
          ON auditcore.p2_task_events (tenant_id, actor_id, created_at_utc);

        -- The live-notify channel carries "<tenant>|<journey>": a Task Manager
        -- task has no Journey and no Journey screen to refresh.
        CREATE OR REPLACE FUNCTION auditcore.trg_p2_tasks_notify_fn() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.journey_id IS NULL THEN
            RETURN NULL;
          END IF;
          IF TG_OP = 'INSERT' OR OLD.task_status IS DISTINCT FROM NEW.task_status THEN
            PERFORM pg_notify('p2_journey', NEW.tenant_id || '|' || NEW.journey_id::text);
          END IF;
          RETURN NULL;
        END;
        $$;

        CREATE TABLE IF NOT EXISTS auditcore.p2_task_schedules (
          tenant_id               varchar(128) NOT NULL,
          schedule_id             uuid NOT NULL DEFAULT gen_random_uuid(),
          system_kind             varchar(40),
          title                   varchar(300) NOT NULL,
          description             text NOT NULL,
          category                varchar(120) NOT NULL DEFAULT 'RECURRING',
          priority                varchar(20) NOT NULL DEFAULT 'NORMAL'
                                  CHECK (priority IN ('URGENT', 'HIGH', 'NORMAL', 'LOW')),
          frequency               varchar(20) NOT NULL CHECK (frequency IN ('DAILY', 'WEEKLY', 'MONTHLY')),
          weekdays                smallint[] NOT NULL DEFAULT ARRAY[1, 2, 3, 4, 5, 6]::smallint[],
          month_day               smallint CHECK (month_day IS NULL OR month_day BETWEEN 1 AND 31),
          run_time_ist            time NOT NULL DEFAULT '09:00',
          expected_close_minutes  integer NOT NULL CHECK (expected_close_minutes > 0),
          assignee_mode           varchar(20) NOT NULL
                                  CHECK (assignee_mode IN ('ACTOR', 'ROLE_EACH', 'OUTLET_PCS')),
          assigned_role_code      varchar(80) NOT NULL,
          assigned_actor_id       varchar(160),
          assigned_display_name   varchar(200),
          outlet_id               uuid,
          starts_on               date NOT NULL DEFAULT CURRENT_DATE,
          ends_on                 date,
          skip_dates              date[] NOT NULL DEFAULT ARRAY[]::date[],
          schedule_status         varchar(20) NOT NULL DEFAULT 'ACTIVE'
                                  CHECK (schedule_status IN ('ACTIVE', 'PAUSED', 'ARCHIVED')),
          created_by_actor_id     varchar(160),
          created_by_role_code    varchar(80),
          created_by_display_name varchar(200),
          created_at_utc          timestamptz NOT NULL DEFAULT now(),
          updated_at_utc          timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, schedule_id),
          CHECK (frequency <> 'MONTHLY' OR month_day IS NOT NULL),
          CHECK (assignee_mode <> 'ACTOR' OR assigned_actor_id IS NOT NULL)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_p2_task_schedules_system
          ON auditcore.p2_task_schedules (tenant_id, system_kind)
          WHERE system_kind IS NOT NULL;

        ALTER TABLE auditcore.p2_task_schedules ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_task_schedules FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation_p2_task_schedules ON auditcore.p2_task_schedules;
        CREATE POLICY tenant_isolation_p2_task_schedules ON auditcore.p2_task_schedules
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());
        GRANT SELECT, INSERT, UPDATE ON auditcore.p2_task_schedules TO audit_core_runtime;

        CREATE TABLE IF NOT EXISTS auditcore.p2_user_activity_daily (
          tenant_id             varchar(128) NOT NULL,
          actor_id              varchar(160) NOT NULL,
          activity_date         date NOT NULL,
          client_kind           varchar(20) NOT NULL DEFAULT 'WEB'
                                CHECK (client_kind IN ('WEB', 'ANDROID', 'IOS')),
          role_code             varchar(80),
          display_name          varchar(200),
          active_seconds        integer NOT NULL DEFAULT 0 CHECK (active_seconds >= 0),
          idle_seconds          integer NOT NULL DEFAULT 0 CHECK (idle_seconds >= 0),
          background_seconds    integer NOT NULL DEFAULT 0 CHECK (background_seconds >= 0),
          hourly_active_minutes smallint[] NOT NULL DEFAULT array_fill(0::smallint, ARRAY[24]),
          first_active_at_utc   timestamptz,
          last_active_at_utc    timestamptz,
          created_at_utc        timestamptz NOT NULL DEFAULT now(),
          updated_at_utc        timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (tenant_id, actor_id, activity_date, client_kind)
        );
        CREATE INDEX IF NOT EXISTS ix_p2_user_activity_daily_date
          ON auditcore.p2_user_activity_daily (tenant_id, activity_date);

        ALTER TABLE auditcore.p2_user_activity_daily ENABLE ROW LEVEL SECURITY;
        ALTER TABLE auditcore.p2_user_activity_daily FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation_p2_user_activity_daily ON auditcore.p2_user_activity_daily;
        CREATE POLICY tenant_isolation_p2_user_activity_daily ON auditcore.p2_user_activity_daily
          USING (tenant_id = auditcore.current_tenant_id())
          WITH CHECK (tenant_id = auditcore.current_tenant_id());
        GRANT SELECT, INSERT, UPDATE ON auditcore.p2_user_activity_daily TO audit_core_runtime;

        -- Gate-pass mismatch tasks move into the Task Manager (OUTLET, no reviewer).
        INSERT INTO auditcore.p2_tasks (
          tenant_id, task_id, journey_id, scope_kind, dealer_id, outlet_id,
          task_type, category, origin_kind, source_type, source_code, dedupe_key,
          title, description, reference, severity, priority,
          assigned_role_code, raised_by_actor_id, raised_by_role_code,
          allowed_actions, completion_protocol, task_status,
          created_at_utc, updated_at_utc, verified_at_utc,
          action_completed_at_utc, completed_by_actor_id, completion_result
        )
        SELECT d.tenant_id, d.daily_task_id, NULL, 'OUTLET', d.dealer_id, d.outlet_id,
               d.task_type, d.category, 'SYSTEM', 'GATE_PASS', d.gate_pass_id::text,
               'daily:' || d.daily_task_id::text,
               d.title, d.description,
               d.details || jsonb_build_object(
                 'gatePassId', d.gate_pass_id, 'businessDate', d.business_date, 'outletName', d.outlet_name),
               'HIGH', d.priority,
               d.assigned_role_code, d.raised_by_actor_id, 'PC',
               '["COMPLETE_ACTION","ADD_COMMENT"]'::jsonb, 'SELF_CLOSING',
               CASE WHEN d.task_status = 'DONE' THEN 'VERIFIED_COMPLETE' ELSE 'READY' END,
               d.created_at_utc, COALESCE(d.resolved_at_utc, d.created_at_utc), d.resolved_at_utc,
               d.resolved_at_utc, d.resolved_by_actor_id,
               CASE WHEN d.resolution_note IS NULL THEN '{}'::jsonb
                    ELSE jsonb_build_object('note', d.resolution_note) END
        FROM auditcore.p2_daily_tasks d
        ON CONFLICT (tenant_id, dedupe_key) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS auditcore.p2_user_activity_daily;
        DROP TABLE IF EXISTS auditcore.p2_task_schedules;
        DELETE FROM auditcore.p2_tasks WHERE scope_kind <> 'JOURNEY';
        DELETE FROM auditcore.p2_task_events WHERE journey_id IS NULL;
        ALTER TABLE auditcore.p2_task_events ALTER COLUMN journey_id SET NOT NULL;
        ALTER TABLE auditcore.p2_tasks DROP CONSTRAINT IF EXISTS p2_tasks_scope_kind_check;
        ALTER TABLE auditcore.p2_tasks DROP CONSTRAINT IF EXISTS p2_tasks_expected_close_minutes_check;
        ALTER TABLE auditcore.p2_tasks ALTER COLUMN journey_id SET NOT NULL;
        ALTER TABLE auditcore.p2_tasks DROP CONSTRAINT IF EXISTS p2_tasks_completion_protocol_check;
        ALTER TABLE auditcore.p2_tasks ADD CONSTRAINT p2_tasks_completion_protocol_check
          CHECK (completion_protocol IN ('MACHINE_VERIFIED', 'REQUESTER_CONFIRMED'));
        DROP INDEX IF EXISTS auditcore.ix_p2_tasks_task_manager;
        DROP INDEX IF EXISTS auditcore.ix_p2_tasks_assigned_actor;
        DROP INDEX IF EXISTS auditcore.ix_p2_tasks_occurrence;
        DROP INDEX IF EXISTS auditcore.ix_p2_tasks_completed_at;
        DROP INDEX IF EXISTS auditcore.ix_p2_task_events_actor_time;
        ALTER TABLE auditcore.p2_tasks
          DROP COLUMN IF EXISTS scope_kind, DROP COLUMN IF EXISTS dealer_id, DROP COLUMN IF EXISTS outlet_id,
          DROP COLUMN IF EXISTS expected_close_minutes, DROP COLUMN IF EXISTS reviewer_role_code,
          DROP COLUMN IF EXISTS reviewer_actor_id, DROP COLUMN IF EXISTS review_due_at_utc,
          DROP COLUMN IF EXISTS action_completed_at_utc, DROP COLUMN IF EXISTS completed_by_actor_id,
          DROP COLUMN IF EXISTS schedule_id, DROP COLUMN IF EXISTS occurrence_date,
          DROP COLUMN IF EXISTS missed_at_utc, DROP COLUMN IF EXISTS raised_by_display_name,
          DROP COLUMN IF EXISTS assigned_display_name;
        """
    )
