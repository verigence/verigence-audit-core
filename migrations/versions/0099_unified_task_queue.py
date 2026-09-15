"""Unified Task Queue -- Daily-Ops-capable tasks + task severity.

Revision ID: 0099_unified_task_queue
Revises: 0098_finding_verdicts
Create Date: 2026-09-15

Two independent additive changes, both needed for a single cross-subject
Task Queue (docs/UNIFIED_WORK_ITEMS_DESIGN_v1.1.md's "one queue, not two"
decision, extended per direct instruction to also carry Daily Operations
tasks and Task severity):

1. ``workflow_instances`` / ``workflow_tasks`` / ``workflow_task_events``
   were journey-only (``journey_id uuid NOT NULL``) -- there was no way to
   represent a Take-Action task against a Daily Operations finding, only
   against a journey one. ``audit_findings`` already solved exactly this
   problem for findings back in migration 0080 (nullable ``journey_id`` +
   a sibling ``daily_ops_run_id`` column, ``subject_kind`` discriminating
   the two). This migration applies the identical pattern to the task
   tables: ``journey_id`` becomes nullable, ``daily_ops_run_id`` is added
   to ``workflow_instances``/``workflow_tasks`` (workflow_task_events
   only needs its own ``journey_id`` relaxed -- it's a denormalized copy
   for the audit trail, always reachable via its parent task), and a
   CHECK enforces exactly one of the two is set. Every existing journey-
   scoped call site is unaffected -- it keeps supplying journey_id exactly
   as before; daily_ops_run_id is simply never populated for those rows.

2. ``workflow_tasks.severity`` (and the mirrored ``work_item_task_detail
   .severity``): a Task raised via Take Action already carried a
   TL-set severity, but only inside ``task_payload`` jsonb -- fine for
   display, useless for sorting/filtering a queue the way Findings'
   real ``severity`` column already supports. Promoted to a real column;
   existing rows are backfilled from their own task_payload.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0099_unified_task_queue"
down_revision = "0098_finding_verdicts"
branch_labels = None
depends_on = None

_SUBJECT_CHECK = (
    "(CASE WHEN journey_id IS NOT NULL THEN 1 ELSE 0 END) "
    "+ (CASE WHEN daily_ops_run_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)


def upgrade() -> None:
    conn = op.get_bind()

    # -- workflow_instances: nullable journey_id + daily_ops_run_id --------
    conn.execute(text("ALTER TABLE auditcore.workflow_instances ALTER COLUMN journey_id DROP NOT NULL"))
    conn.execute(text("ALTER TABLE auditcore.workflow_instances ADD COLUMN daily_ops_run_id uuid"))
    conn.execute(
        text(
            "ALTER TABLE auditcore.workflow_instances "
            "ADD CONSTRAINT fk_workflow_instances_daily_ops_run "
            "FOREIGN KEY (tenant_id, daily_ops_run_id) "
            "REFERENCES auditcore.daily_ops_runs(tenant_id, daily_ops_run_id)"
        )
    )
    conn.execute(
        text(
            f"ALTER TABLE auditcore.workflow_instances "
            f"ADD CONSTRAINT ck_workflow_instances_subject CHECK ({_SUBJECT_CHECK})"
        )
    )

    # -- workflow_tasks: nullable journey_id + daily_ops_run_id + severity -
    conn.execute(text("ALTER TABLE auditcore.workflow_tasks ALTER COLUMN journey_id DROP NOT NULL"))
    conn.execute(text("ALTER TABLE auditcore.workflow_tasks ADD COLUMN daily_ops_run_id uuid"))
    conn.execute(text("ALTER TABLE auditcore.workflow_tasks ADD COLUMN severity varchar(20)"))
    conn.execute(
        text(
            "ALTER TABLE auditcore.workflow_tasks "
            "ADD CONSTRAINT fk_workflow_tasks_daily_ops_run "
            "FOREIGN KEY (tenant_id, daily_ops_run_id) "
            "REFERENCES auditcore.daily_ops_runs(tenant_id, daily_ops_run_id)"
        )
    )
    conn.execute(
        text(
            f"ALTER TABLE auditcore.workflow_tasks "
            f"ADD CONSTRAINT ck_workflow_tasks_subject CHECK ({_SUBJECT_CHECK})"
        )
    )
    # Backfill severity for existing Take-Action tasks from their own payload
    # -- the only producer that ever set it there.
    conn.execute(
        text(
            """
            UPDATE auditcore.workflow_tasks
            SET severity = upper(task_payload->>'severity')
            WHERE task_payload ? 'severity'
              AND coalesce(task_payload->>'severity', '') <> ''
              AND severity IS NULL
            """
        )
    )

    # -- workflow_task_events: nullable journey_id (denormalized copy, ------
    # reachable via its parent task -- no daily_ops_run_id column needed) --
    conn.execute(text("ALTER TABLE auditcore.workflow_task_events ALTER COLUMN journey_id DROP NOT NULL"))

    # -- work_item_task_detail: mirror severity + process_area (the latter --
    # was never carried onto the spine at all -- the unified Task Queue
    # needs it for the same BOOKING/DELIVERY/DAILY_OPS stage filter Findings
    # already support via work_item_finding_detail.stage_code).
    conn.execute(text("ALTER TABLE auditcore.work_item_task_detail ADD COLUMN severity varchar(20)"))
    conn.execute(text("ALTER TABLE auditcore.work_item_task_detail ADD COLUMN process_area varchar(80)"))
    conn.execute(
        text(
            """
            UPDATE auditcore.work_item_task_detail wtd
            SET severity = wt.severity, process_area = wt.process_area
            FROM auditcore.workflow_tasks wt
            WHERE wt.tenant_id = wtd.tenant_id AND wt.workflow_task_id = wtd.work_item_id
            """
        )
    )

    # -- re-create sync_task_work_item(), extended for severity + carrying -
    # subject_kind/subject_ref through for a Daily-Ops-scoped task ---------
    conn.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION auditcore.sync_task_work_item()
            RETURNS trigger AS $$
            BEGIN
              BEGIN
                INSERT INTO auditcore.work_items (
                  tenant_id, work_item_id, item_kind, origin_kind, subject_kind,
                  subject_ref, classification, owner_role_code, assigned_actor_id,
                  priority, due_at_utc, status, title, created_by_actor_id,
                  created_at_utc, updated_at_utc, version_no, correlation_id
                ) VALUES (
                  NEW.tenant_id, NEW.workflow_task_id, 'EXECUTION_TASK', 'SYSTEM',
                  CASE WHEN NEW.daily_ops_run_id IS NOT NULL THEN 'DAILY_OPS' ELSE 'JOURNEY' END,
                  COALESCE(NEW.journey_id, NEW.daily_ops_run_id), 'EXECUTION',
                  NEW.assigned_role_code, NEW.assigned_actor_id,
                  NEW.priority, NEW.due_at_utc,
                  CASE NEW.task_status
                    WHEN 'PENDING' THEN 'OPEN'
                    WHEN 'READY' THEN 'OPEN'
                    WHEN 'CLAIMED' THEN 'IN_PROGRESS'
                    WHEN 'IN_PROGRESS' THEN 'IN_PROGRESS'
                    WHEN 'RETRY_WAIT' THEN 'IN_PROGRESS'
                    WHEN 'COMPLETED' THEN 'RESOLVED'
                    WHEN 'FAILED' THEN 'CANCELLED'
                    WHEN 'CANCELLED' THEN 'CANCELLED'
                    WHEN 'DEAD_LETTER' THEN 'CANCELLED'
                    ELSE 'OPEN'
                  END,
                  NEW.task_type, NEW.assigned_actor_id,
                  NEW.created_at_utc, NEW.updated_at_utc, NEW.version_no,
                  NEW.correlation_id
                )
                ON CONFLICT (tenant_id, work_item_id) DO UPDATE SET
                  subject_kind = EXCLUDED.subject_kind,
                  subject_ref = EXCLUDED.subject_ref,
                  owner_role_code = EXCLUDED.owner_role_code,
                  assigned_actor_id = EXCLUDED.assigned_actor_id,
                  priority = EXCLUDED.priority,
                  due_at_utc = EXCLUDED.due_at_utc,
                  status = EXCLUDED.status,
                  title = EXCLUDED.title,
                  updated_at_utc = EXCLUDED.updated_at_utc,
                  version_no = EXCLUDED.version_no;

                INSERT INTO auditcore.work_item_task_detail (
                  tenant_id, work_item_id, task_type, effect_key, attempt_count,
                  max_attempts, next_attempt_at_utc, lease_owner,
                  lease_acquired_at_utc, lease_expires_at_utc, task_payload,
                  last_error_code, last_error_summary, related_finding_id,
                  severity, process_area
                ) VALUES (
                  NEW.tenant_id, NEW.workflow_task_id, NEW.task_type,
                  NEW.effect_key, NEW.attempt_count, NEW.max_attempts,
                  NEW.next_attempt_at_utc, NEW.lease_owner,
                  NEW.lease_acquired_at_utc, NEW.lease_expires_at_utc,
                  NEW.task_payload, NEW.last_error_code, NEW.last_error_summary,
                  NEW.related_finding_id, NEW.severity, NEW.process_area
                )
                ON CONFLICT (tenant_id, work_item_id) DO UPDATE SET
                  attempt_count = EXCLUDED.attempt_count,
                  max_attempts = EXCLUDED.max_attempts,
                  next_attempt_at_utc = EXCLUDED.next_attempt_at_utc,
                  lease_owner = EXCLUDED.lease_owner,
                  lease_acquired_at_utc = EXCLUDED.lease_acquired_at_utc,
                  lease_expires_at_utc = EXCLUDED.lease_expires_at_utc,
                  task_payload = EXCLUDED.task_payload,
                  last_error_code = EXCLUDED.last_error_code,
                  last_error_summary = EXCLUDED.last_error_summary,
                  related_finding_id = EXCLUDED.related_finding_id,
                  severity = EXCLUDED.severity,
                  process_area = EXCLUDED.process_area;
              EXCEPTION WHEN OTHERS THEN
                RAISE WARNING 'work_items mirror failed for workflow_task_id=%: %',
                  NEW.workflow_task_id, SQLERRM;
              END;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("ALTER TABLE auditcore.work_item_task_detail DROP COLUMN IF EXISTS process_area"))
    conn.execute(text("ALTER TABLE auditcore.work_item_task_detail DROP COLUMN IF EXISTS severity"))
    conn.execute(text("ALTER TABLE auditcore.workflow_task_events ALTER COLUMN journey_id SET NOT NULL"))
    conn.execute(text("ALTER TABLE auditcore.workflow_tasks DROP CONSTRAINT IF EXISTS ck_workflow_tasks_subject"))
    conn.execute(text("ALTER TABLE auditcore.workflow_tasks DROP CONSTRAINT IF EXISTS fk_workflow_tasks_daily_ops_run"))
    conn.execute(text("ALTER TABLE auditcore.workflow_tasks DROP COLUMN IF EXISTS severity"))
    conn.execute(text("ALTER TABLE auditcore.workflow_tasks DROP COLUMN IF EXISTS daily_ops_run_id"))
    conn.execute(text("ALTER TABLE auditcore.workflow_tasks ALTER COLUMN journey_id SET NOT NULL"))
    conn.execute(text("ALTER TABLE auditcore.workflow_instances DROP CONSTRAINT IF EXISTS ck_workflow_instances_subject"))
    conn.execute(text("ALTER TABLE auditcore.workflow_instances DROP CONSTRAINT IF EXISTS fk_workflow_instances_daily_ops_run"))
    conn.execute(text("ALTER TABLE auditcore.workflow_instances DROP COLUMN IF EXISTS daily_ops_run_id"))
    conn.execute(text("ALTER TABLE auditcore.workflow_instances ALTER COLUMN journey_id SET NOT NULL"))

    # sync_task_work_item() reverts to the 0098 shape (no severity/subject_kind).
    conn.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION auditcore.sync_task_work_item()
            RETURNS trigger AS $$
            BEGIN
              BEGIN
                INSERT INTO auditcore.work_items (
                  tenant_id, work_item_id, item_kind, origin_kind, subject_kind,
                  subject_ref, classification, owner_role_code, assigned_actor_id,
                  priority, due_at_utc, status, title, created_by_actor_id,
                  created_at_utc, updated_at_utc, version_no, correlation_id
                ) VALUES (
                  NEW.tenant_id, NEW.workflow_task_id, 'EXECUTION_TASK', 'SYSTEM',
                  'JOURNEY', NEW.journey_id, 'EXECUTION',
                  NEW.assigned_role_code, NEW.assigned_actor_id,
                  NEW.priority, NEW.due_at_utc,
                  CASE NEW.task_status
                    WHEN 'PENDING' THEN 'OPEN'
                    WHEN 'READY' THEN 'OPEN'
                    WHEN 'CLAIMED' THEN 'IN_PROGRESS'
                    WHEN 'IN_PROGRESS' THEN 'IN_PROGRESS'
                    WHEN 'RETRY_WAIT' THEN 'IN_PROGRESS'
                    WHEN 'COMPLETED' THEN 'RESOLVED'
                    WHEN 'FAILED' THEN 'CANCELLED'
                    WHEN 'CANCELLED' THEN 'CANCELLED'
                    WHEN 'DEAD_LETTER' THEN 'CANCELLED'
                    ELSE 'OPEN'
                  END,
                  NEW.task_type, NEW.assigned_actor_id,
                  NEW.created_at_utc, NEW.updated_at_utc, NEW.version_no,
                  NEW.correlation_id
                )
                ON CONFLICT (tenant_id, work_item_id) DO UPDATE SET
                  owner_role_code = EXCLUDED.owner_role_code,
                  assigned_actor_id = EXCLUDED.assigned_actor_id,
                  priority = EXCLUDED.priority,
                  due_at_utc = EXCLUDED.due_at_utc,
                  status = EXCLUDED.status,
                  title = EXCLUDED.title,
                  updated_at_utc = EXCLUDED.updated_at_utc,
                  version_no = EXCLUDED.version_no;

                INSERT INTO auditcore.work_item_task_detail (
                  tenant_id, work_item_id, task_type, effect_key, attempt_count,
                  max_attempts, next_attempt_at_utc, lease_owner,
                  lease_acquired_at_utc, lease_expires_at_utc, task_payload,
                  last_error_code, last_error_summary, related_finding_id
                ) VALUES (
                  NEW.tenant_id, NEW.workflow_task_id, NEW.task_type,
                  NEW.effect_key, NEW.attempt_count, NEW.max_attempts,
                  NEW.next_attempt_at_utc, NEW.lease_owner,
                  NEW.lease_acquired_at_utc, NEW.lease_expires_at_utc,
                  NEW.task_payload, NEW.last_error_code, NEW.last_error_summary,
                  NEW.related_finding_id
                )
                ON CONFLICT (tenant_id, work_item_id) DO UPDATE SET
                  attempt_count = EXCLUDED.attempt_count,
                  max_attempts = EXCLUDED.max_attempts,
                  next_attempt_at_utc = EXCLUDED.next_attempt_at_utc,
                  lease_owner = EXCLUDED.lease_owner,
                  lease_acquired_at_utc = EXCLUDED.lease_acquired_at_utc,
                  lease_expires_at_utc = EXCLUDED.lease_expires_at_utc,
                  task_payload = EXCLUDED.task_payload,
                  last_error_code = EXCLUDED.last_error_code,
                  last_error_summary = EXCLUDED.last_error_summary,
                  related_finding_id = EXCLUDED.related_finding_id;
              EXCEPTION WHEN OTHERS THEN
                RAISE WARNING 'work_items mirror failed for workflow_task_id=%: %',
                  NEW.workflow_task_id, SQLERRM;
              END;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
