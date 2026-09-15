"""Unified Work Items -- Phase 1: spine + detail tables, mirrored by trigger.

Revision ID: 0097_unified_work_items
Revises: 0096_uc03_payment_mode_types
Create Date: 2026-09-15

See docs/UNIFIED_WORK_ITEMS_DESIGN_v1.0.md. Finding creation/update is
scattered across 13+ producer files (grepped directly, not assumed); a
database trigger mirrors every INSERT/UPDATE on audit_findings and
workflow_tasks into the new work_items spine + a kind-specific detail
table, without requiring a single one of those files to change. Nothing
reads from work_items yet -- this is pure additive instrumentation, zero
behavior change to any existing endpoint. The trigger body is wrapped in
its own exception handler so a mirror bug can never block or fail the
real write it's shadowing.

Column mapping decisions, each verified against the actual current
INSERT statements (not the original v1.0/v2.1 schema file, which predates
several migrations that added columns):

- origin_kind: audit_findings today stores 'MACHINE' | 'RULE' | 'HUMAN'
  (confirmed in uc03_delivery_commands.py's _machine_flag and
  uc03_confidence_review_policy.py's Manual Verification insert). 'RULE'
  is the Manual Verification / DI-confidence case, which is genuinely a
  SYSTEM-originated decision, not a declarative rule -- mapped to the new
  vocabulary's 'SYSTEM', not kept as a confusing third literal.
- subject_kind: audit_findings already has its own subject_kind column
  (migration 0080_daily_ops_findings) -- CHECK-constrained to 'JOURNEY' |
  'DAILY_OPS', with journey_id/daily_ops_run_id set exactly per which one.
  Used directly rather than re-derived from stage_code. workflow_tasks has
  no daily-ops concept today (journey_id is NOT NULL on that table) so
  every task row is 'JOURNEY'.
- status: OPEN/IN_PROGRESS/RESOLVED/CANCELLED per the design doc's
  decided vocabulary -- see "Decided" section there for why this wording
  was picked over the existing ACKNOWLEDGED/VOIDED words.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0097_unified_work_items"
down_revision = "0096_uc03_payment_mode_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        text(
            """
            CREATE TABLE auditcore.work_items (
              tenant_id           varchar(128) NOT NULL,
              work_item_id        uuid NOT NULL,
              item_kind           varchar(20)  NOT NULL
                                   CHECK (item_kind IN ('FINDING','EXECUTION_TASK')),
              origin_kind         varchar(20)  NOT NULL
                                   CHECK (origin_kind IN ('MACHINE','SYSTEM','HUMAN')),
              subject_kind        varchar(20)  NOT NULL
                                   CHECK (subject_kind IN ('JOURNEY','DAILY_OPS','GENERAL')),
              subject_ref         uuid,
              classification      varchar(40),
              owner_role_code     varchar(80),
              assigned_actor_id   varchar(160),
              priority            integer NOT NULL DEFAULT 50,
              due_at_utc          timestamptz,
              status              varchar(20)  NOT NULL DEFAULT 'OPEN'
                                   CHECK (status IN ('OPEN','IN_PROGRESS','RESOLVED','CANCELLED')),
              title               varchar(500) NOT NULL,
              summary             text,
              created_by_actor_id varchar(160),
              created_at_utc      timestamptz NOT NULL DEFAULT now(),
              updated_at_utc      timestamptz NOT NULL DEFAULT now(),
              version_no          bigint NOT NULL DEFAULT 1,
              correlation_id      varchar(128),
              PRIMARY KEY (tenant_id, work_item_id)
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE INDEX ix_work_items_queue ON auditcore.work_items"
            "(tenant_id, subject_kind, status, priority DESC, due_at_utc)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX ix_work_items_owner ON auditcore.work_items"
            "(tenant_id, assigned_actor_id, status)"
        )
    )

    conn.execute(
        text(
            """
            CREATE TABLE auditcore.work_item_finding_detail (
              tenant_id            varchar(128) NOT NULL,
              work_item_id         uuid NOT NULL,
              rule_key             varchar(120),
              rule_version_id      uuid,
              severity             varchar(20),
              expected_summary     text,
              observed_summary     text,
              resolution_reason    text,
              disposition          varchar(30),
              blocking_completion  boolean NOT NULL DEFAULT false,
              stage_code           varchar(20),
              PRIMARY KEY (tenant_id, work_item_id),
              FOREIGN KEY (tenant_id, work_item_id)
                REFERENCES auditcore.work_items(tenant_id, work_item_id)
            )
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TABLE auditcore.work_item_task_detail (
              tenant_id              varchar(128) NOT NULL,
              work_item_id           uuid NOT NULL,
              task_type              varchar(120) NOT NULL,
              effect_key             varchar(240),
              attempt_count          integer NOT NULL DEFAULT 0,
              max_attempts           integer NOT NULL DEFAULT 5,
              next_attempt_at_utc    timestamptz,
              lease_owner            varchar(200),
              lease_acquired_at_utc  timestamptz,
              lease_expires_at_utc   timestamptz,
              task_payload           jsonb NOT NULL DEFAULT '{}'::jsonb,
              last_error_code        varchar(80),
              last_error_summary     text,
              PRIMARY KEY (tenant_id, work_item_id),
              FOREIGN KEY (tenant_id, work_item_id)
                REFERENCES auditcore.work_items(tenant_id, work_item_id)
            )
            """
        )
    )

    # -- audit_findings mirror -------------------------------------------------
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
                  -- audit_findings.subject_kind (migration 0080) is already the
                  -- authoritative discriminator -- 'JOURNEY' | 'DAILY_OPS', CHECK-
                  -- constrained -- use it directly rather than inferring from
                  -- stage_code (which happens to correlate, but isn't the real
                  -- source of truth).
                  NEW.subject_kind,
                  COALESCE(NEW.journey_id, NEW.daily_ops_run_id),
                  -- audit_findings has never tracked a specific assignee, only
                  -- an owning role (owner_role_code) -- leave assigned_actor_id
                  -- null rather than borrowing origin_actor_id (who raised it,
                  -- not who owns resolving it; already separately captured as
                  -- created_by_actor_id below).
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
                  disposition, blocking_completion, stage_code
                ) VALUES (
                  NEW.tenant_id, NEW.audit_finding_id, NEW.rule_key,
                  NEW.rule_version_id, NEW.severity, NEW.expected_summary,
                  NEW.observed_summary, NEW.resolution_reason, NEW.disposition,
                  NEW.blocking_completion, NEW.stage_code
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
                  stage_code = EXCLUDED.stage_code;
              EXCEPTION WHEN OTHERS THEN
                RAISE WARNING 'work_items mirror failed for audit_finding_id=%: %',
                  NEW.audit_finding_id, SQLERRM;
              END;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE TRIGGER trg_sync_finding_work_item
            AFTER INSERT OR UPDATE ON auditcore.audit_findings
            FOR EACH ROW EXECUTE FUNCTION auditcore.sync_finding_work_item()
            """
        )
    )

    # -- workflow_tasks mirror --------------------------------------------------
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
                  last_error_code, last_error_summary
                ) VALUES (
                  NEW.tenant_id, NEW.workflow_task_id, NEW.task_type,
                  NEW.effect_key, NEW.attempt_count, NEW.max_attempts,
                  NEW.next_attempt_at_utc, NEW.lease_owner,
                  NEW.lease_acquired_at_utc, NEW.lease_expires_at_utc,
                  NEW.task_payload, NEW.last_error_code, NEW.last_error_summary
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
                  last_error_summary = EXCLUDED.last_error_summary;
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
    conn.execute(
        text(
            """
            CREATE TRIGGER trg_sync_task_work_item
            AFTER INSERT OR UPDATE ON auditcore.workflow_tasks
            FOR EACH ROW EXECUTE FUNCTION auditcore.sync_task_work_item()
            """
        )
    )

    # -- one-time backfill of existing rows (same mapping as the triggers) -----
    conn.execute(
        text(
            """
            INSERT INTO auditcore.work_items (
              tenant_id, work_item_id, item_kind, origin_kind, subject_kind,
              subject_ref, classification, owner_role_code, assigned_actor_id,
              due_at_utc, status, title, summary, created_by_actor_id,
              created_at_utc, updated_at_utc, version_no, correlation_id
            )
            SELECT
              tenant_id, audit_finding_id, 'FINDING',
              CASE origin_kind
                WHEN 'MACHINE' THEN 'MACHINE' WHEN 'RULE' THEN 'SYSTEM'
                WHEN 'HUMAN' THEN 'HUMAN' ELSE 'MACHINE'
              END,
              subject_kind,
              COALESCE(journey_id, daily_ops_run_id),
              finding_class, owner_role_code, NULL, sla_due_at_utc,
              CASE finding_status
                WHEN 'OPEN' THEN 'OPEN' WHEN 'ACKNOWLEDGED' THEN 'IN_PROGRESS'
                WHEN 'RESOLVED' THEN 'RESOLVED' WHEN 'VOIDED' THEN 'CANCELLED'
                ELSE 'OPEN'
              END,
              title, description, created_by_actor_id,
              created_at_utc, updated_at_utc, version_no, correlation_id
            FROM auditcore.audit_findings
            ON CONFLICT (tenant_id, work_item_id) DO NOTHING
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO auditcore.work_item_finding_detail (
              tenant_id, work_item_id, rule_key, rule_version_id, severity,
              expected_summary, observed_summary, resolution_reason,
              disposition, blocking_completion, stage_code
            )
            SELECT
              tenant_id, audit_finding_id, rule_key, rule_version_id, severity,
              expected_summary, observed_summary, resolution_reason,
              disposition, blocking_completion, stage_code
            FROM auditcore.audit_findings
            ON CONFLICT (tenant_id, work_item_id) DO NOTHING
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO auditcore.work_items (
              tenant_id, work_item_id, item_kind, origin_kind, subject_kind,
              subject_ref, classification, owner_role_code, assigned_actor_id,
              priority, due_at_utc, status, title, created_by_actor_id,
              created_at_utc, updated_at_utc, version_no, correlation_id
            )
            SELECT
              tenant_id, workflow_task_id, 'EXECUTION_TASK', 'SYSTEM', 'JOURNEY',
              journey_id, 'EXECUTION', assigned_role_code, assigned_actor_id,
              priority, due_at_utc,
              CASE task_status
                WHEN 'PENDING' THEN 'OPEN' WHEN 'READY' THEN 'OPEN'
                WHEN 'CLAIMED' THEN 'IN_PROGRESS' WHEN 'IN_PROGRESS' THEN 'IN_PROGRESS'
                WHEN 'RETRY_WAIT' THEN 'IN_PROGRESS' WHEN 'COMPLETED' THEN 'RESOLVED'
                WHEN 'FAILED' THEN 'CANCELLED' WHEN 'CANCELLED' THEN 'CANCELLED'
                WHEN 'DEAD_LETTER' THEN 'CANCELLED' ELSE 'OPEN'
              END,
              task_type, assigned_actor_id,
              created_at_utc, updated_at_utc, version_no, correlation_id
            FROM auditcore.workflow_tasks
            ON CONFLICT (tenant_id, work_item_id) DO NOTHING
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO auditcore.work_item_task_detail (
              tenant_id, work_item_id, task_type, effect_key, attempt_count,
              max_attempts, next_attempt_at_utc, lease_owner,
              lease_acquired_at_utc, lease_expires_at_utc, task_payload,
              last_error_code, last_error_summary
            )
            SELECT
              tenant_id, workflow_task_id, task_type, effect_key, attempt_count,
              max_attempts, next_attempt_at_utc, lease_owner,
              lease_acquired_at_utc, lease_expires_at_utc, task_payload,
              last_error_code, last_error_summary
            FROM auditcore.workflow_tasks
            ON CONFLICT (tenant_id, work_item_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP TRIGGER IF EXISTS trg_sync_task_work_item ON auditcore.workflow_tasks"))
    conn.execute(text("DROP TRIGGER IF EXISTS trg_sync_finding_work_item ON auditcore.audit_findings"))
    conn.execute(text("DROP FUNCTION IF EXISTS auditcore.sync_task_work_item()"))
    conn.execute(text("DROP FUNCTION IF EXISTS auditcore.sync_finding_work_item()"))
    conn.execute(text("DROP TABLE IF EXISTS auditcore.work_item_task_detail"))
    conn.execute(text("DROP TABLE IF EXISTS auditcore.work_item_finding_detail"))
    conn.execute(text("DROP TABLE IF EXISTS auditcore.work_items"))
