"""Finding verdicts (Accept/Reject/Take Action/Escalate) -- schema for v1.1.

Revision ID: 0098_finding_verdicts
Revises: 0097_unified_work_items
Create Date: 2026-09-15

See docs/UNIFIED_WORK_ITEMS_DESIGN_v1.1.md. Application-side changes
(uc03_audit_flags.py's new TAKE_ACTION/ESCALATE actions,
uc03_finding_routing.py's permitted_actions, the _machine_flag guard
against touching a mid-human-loop finding) ship in the same PR as this
migration -- this file is schema only.

Three additive columns:

- ``audit_findings.rejection_category``: the required category dropdown
  decided alongside the existing 50-word free-text remark for Reject
  (MARK_FALSE_POSITIVE). Application-validated against a closed set
  (uc03_audit_flags.py); a plain varchar here, not a DB CHECK, to match
  this table's existing convention (severity/finding_status use CHECK,
  but disposition's own sibling categorical columns like origin_kind
  already lean on application validation for anything this
  presentation-facing).
- ``audit_findings.escalation_priority``: set to 'HIGH' when a Finding
  is Escalated to PM. Deliberately separate from ``severity`` -- the
  design decided escalation is a priority signal on the escalation
  itself, never an overwrite of the rule's own original severity
  assessment.
- ``workflow_tasks.related_finding_id``: traces a Task back to the
  Finding that produced it. The one existing satellite-task mechanism
  (PC_DOCUMENT_REUPLOAD) has no such link today -- a real gap the design
  called out, closed here for every Task going forward.

The Unified Work Items mirror trigger (migration 0097) is updated to
carry these three columns into ``work_items``/``work_item_finding_detail``/
``work_item_task_detail`` too, so the spine doesn't silently fall behind
the tables it mirrors.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0098_finding_verdicts"
down_revision = "0097_unified_work_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        text(
            "ALTER TABLE auditcore.audit_findings "
            "ADD COLUMN rejection_category varchar(40), "
            "ADD COLUMN escalation_priority varchar(20)"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE auditcore.workflow_tasks "
            "ADD COLUMN related_finding_id uuid"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE auditcore.workflow_tasks "
            "ADD CONSTRAINT fk_workflow_tasks_related_finding "
            "FOREIGN KEY (tenant_id, related_finding_id) "
            "REFERENCES auditcore.audit_findings(tenant_id, audit_finding_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX ix_workflow_tasks_related_finding "
            "ON auditcore.workflow_tasks(tenant_id, related_finding_id) "
            "WHERE related_finding_id IS NOT NULL"
        )
    )

    # -- extend the work_items spine to carry the same three columns --------
    conn.execute(
        text(
            "ALTER TABLE auditcore.work_item_finding_detail "
            "ADD COLUMN rejection_category varchar(40), "
            "ADD COLUMN escalation_priority varchar(20)"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE auditcore.work_item_task_detail "
            "ADD COLUMN related_finding_id uuid"
        )
    )

    # -- re-create both mirror trigger functions, extended for the new columns
    conn.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION auditcore.sync_finding_work_item()
            RETURNS trigger AS $$
            BEGIN
              BEGIN
                INSERT INTO auditcore.work_items (
                  tenant_id, work_item_id, item_kind, origin_kind, subject_kind,
                  subject_ref, classification, owner_role_code, assigned_actor_id,
                  due_at_utc, status, title, summary, created_by_actor_id,
                  created_at_utc, updated_at_utc, version_no, correlation_id
                ) VALUES (
                  NEW.tenant_id, NEW.audit_finding_id, 'FINDING',
                  CASE NEW.origin_kind
                    WHEN 'MACHINE' THEN 'MACHINE'
                    WHEN 'RULE' THEN 'SYSTEM'
                    WHEN 'HUMAN' THEN 'HUMAN'
                    ELSE 'MACHINE'
                  END,
                  CASE
                    WHEN NEW.stage_code = 'DAILY_OPS' THEN 'DAILY_OPS'
                    WHEN NEW.stage_code IN ('BOOKING','DELIVERY') THEN 'JOURNEY'
                    ELSE 'GENERAL'
                  END,
                  COALESCE(NEW.journey_id, NEW.daily_ops_run_id),
                  NEW.finding_class, NEW.owner_role_code, NULL,
                  NEW.sla_due_at_utc,
                  CASE NEW.finding_status
                    WHEN 'OPEN' THEN 'OPEN'
                    WHEN 'ACKNOWLEDGED' THEN 'IN_PROGRESS'
                    WHEN 'RESOLVED' THEN 'RESOLVED'
                    WHEN 'VOIDED' THEN 'CANCELLED'
                    ELSE 'OPEN'
                  END,
                  NEW.title, NEW.description, NEW.created_by_actor_id,
                  NEW.created_at_utc, NEW.updated_at_utc, NEW.version_no,
                  NEW.correlation_id
                )
                ON CONFLICT (tenant_id, work_item_id) DO UPDATE SET
                  origin_kind = EXCLUDED.origin_kind,
                  subject_kind = EXCLUDED.subject_kind,
                  subject_ref = EXCLUDED.subject_ref,
                  classification = EXCLUDED.classification,
                  owner_role_code = EXCLUDED.owner_role_code,
                  assigned_actor_id = EXCLUDED.assigned_actor_id,
                  due_at_utc = EXCLUDED.due_at_utc,
                  status = EXCLUDED.status,
                  title = EXCLUDED.title,
                  summary = EXCLUDED.summary,
                  updated_at_utc = EXCLUDED.updated_at_utc,
                  version_no = EXCLUDED.version_no;

                INSERT INTO auditcore.work_item_finding_detail (
                  tenant_id, work_item_id, rule_key, rule_version_id, severity,
                  expected_summary, observed_summary, resolution_reason,
                  disposition, blocking_completion, stage_code,
                  rejection_category, escalation_priority
                ) VALUES (
                  NEW.tenant_id, NEW.audit_finding_id, NEW.rule_key,
                  NEW.rule_version_id, NEW.severity, NEW.expected_summary,
                  NEW.observed_summary, NEW.resolution_reason, NEW.disposition,
                  NEW.blocking_completion, NEW.stage_code,
                  NEW.rejection_category, NEW.escalation_priority
                )
                ON CONFLICT (tenant_id, work_item_id) DO UPDATE SET
                  rule_key = EXCLUDED.rule_key,
                  rule_version_id = EXCLUDED.rule_version_id,
                  severity = EXCLUDED.severity,
                  expected_summary = EXCLUDED.expected_summary,
                  observed_summary = EXCLUDED.observed_summary,
                  resolution_reason = EXCLUDED.resolution_reason,
                  disposition = EXCLUDED.disposition,
                  blocking_completion = EXCLUDED.blocking_completion,
                  stage_code = EXCLUDED.stage_code,
                  rejection_category = EXCLUDED.rejection_category,
                  escalation_priority = EXCLUDED.escalation_priority;
              EXCEPTION WHEN OTHERS THEN
                RAISE WARNING 'work_items mirror failed for audit_finding_id=%: %',
                  NEW.audit_finding_id, SQLERRM;
              END;

              -- Race-condition guardrail (v1.1 design): closing a Finding
              -- always cancels any of its still-open Tasks. Deliberately a
              -- real, unconditional effect here (not wrapped in the mirror's
              -- own best-effort exception block above) -- this is a genuine
              -- business rule that must apply no matter which of this
              -- codebase's several finding-resolving code paths closes the
              -- finding (a human verdict, or any automated self-serve
              -- auto-resolver), not just the mirror's own shadow copy.
              IF TG_OP = 'UPDATE'
                 AND NEW.finding_status IN ('RESOLVED','VOIDED')
                 AND OLD.finding_status IS DISTINCT FROM NEW.finding_status
              THEN
                UPDATE auditcore.workflow_tasks
                SET task_status='CANCELLED', cancelled_at_utc=now(),
                    cancel_reason='Finding closed', version_no=version_no+1
                WHERE tenant_id=NEW.tenant_id AND related_finding_id=NEW.audit_finding_id
                  AND task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT');
              END IF;

              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
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


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("ALTER TABLE auditcore.work_item_task_detail DROP COLUMN IF EXISTS related_finding_id"))
    conn.execute(
        text(
            "ALTER TABLE auditcore.work_item_finding_detail "
            "DROP COLUMN IF EXISTS rejection_category, "
            "DROP COLUMN IF EXISTS escalation_priority"
        )
    )
    conn.execute(text("ALTER TABLE auditcore.workflow_tasks DROP CONSTRAINT IF EXISTS fk_workflow_tasks_related_finding"))
    conn.execute(text("ALTER TABLE auditcore.workflow_tasks DROP COLUMN IF EXISTS related_finding_id"))
    conn.execute(
        text(
            "ALTER TABLE auditcore.audit_findings "
            "DROP COLUMN IF EXISTS rejection_category, "
            "DROP COLUMN IF EXISTS escalation_priority"
        )
    )
