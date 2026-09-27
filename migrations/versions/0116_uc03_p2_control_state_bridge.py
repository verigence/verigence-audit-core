"""0116_uc03_p2_control_state_bridge — mirror existing rule outcomes into P2.

Audit Core and the external Rule Engine continue to execute rules exactly as
they do today. Their shared auditcore.rule_executions log is the factual event
source. P2 mirrors each execution into one latest-state row and wakes any
machine-verification task waiting on that exact rule.

SKIPPED is deliberately conservative: P2 stores WAITING_FOR_FACTS rather than
claiming NOT_APPLICABLE because the legacy execution log does not carry a
machine-readable skip classification. NOT_APPLICABLE remains available for a
future explicit applicability contract.
"""
from __future__ import annotations

from alembic import op

revision = "0116_uc03_p2_control_bridge"
down_revision = "0115_uc03_p2_event_triggers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE auditcore.p2_control_state
          ADD COLUMN source_outcome varchar(20)
            CHECK (source_outcome IS NULL OR source_outcome IN ('PASS','FAIL','SKIPPED','ERROR')),
          ADD COLUMN status_reason text;

        CREATE OR REPLACE FUNCTION auditcore.p2_mirror_rule_execution()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_executor varchar(30);
            v_status varchar(40);
            v_fact_version bigint;
        BEGIN
            -- The legacy rule_executions table intentionally has no Journey FK
            -- (tests and historical tooling can write synthetic execution rows).
            -- P2 must never make those pre-existing writes fail.
            IF NOT EXISTS (
                SELECT 1
                FROM auditcore.journeys j
                WHERE j.tenant_id=NEW.tenant_id
                  AND j.journey_id=NEW.journey_id
            ) THEN
                RETURN NEW;
            END IF;

            SELECT CASE
                     WHEN rd.executor = 'AUDIT_CORE' THEN 'NATIVE'
                     ELSE 'RULE_ENGINE'
                   END
            INTO v_executor
            FROM auditcore.rule_definitions rd
            WHERE rd.rule_code = NEW.rule_code;

            IF v_executor IS NULL THEN
                v_executor := 'RULE_ENGINE';
            END IF;

            v_status := CASE NEW.outcome
                WHEN 'PASS' THEN 'PASS'
                WHEN 'FAIL' THEN 'FAIL'
                WHEN 'ERROR' THEN 'RETRY_PENDING'
                WHEN 'SKIPPED' THEN 'WAITING_FOR_FACTS'
                ELSE 'ERROR_TERMINAL'
            END;

            SELECT fact_version
            INTO v_fact_version
            FROM auditcore.p2_journey_runtime
            WHERE tenant_id=NEW.tenant_id AND journey_id=NEW.journey_id;

            INSERT INTO auditcore.p2_control_state (
                tenant_id, journey_id, control_code, executor_type,
                control_status, evaluated_fact_version, finding_id,
                last_error, last_evaluated_at_utc,
                source_outcome, status_reason, updated_at_utc
            ) VALUES (
                NEW.tenant_id, NEW.journey_id, NEW.rule_code, v_executor,
                v_status, v_fact_version, NEW.audit_finding_id,
                CASE WHEN NEW.outcome='ERROR' THEN NEW.reason ELSE NULL END,
                NEW.evaluated_at_utc,
                NEW.outcome, NEW.reason, now()
            )
            ON CONFLICT (tenant_id, journey_id, control_code)
            DO UPDATE SET
                executor_type=EXCLUDED.executor_type,
                control_status=EXCLUDED.control_status,
                evaluated_fact_version=EXCLUDED.evaluated_fact_version,
                finding_id=EXCLUDED.finding_id,
                last_error=EXCLUDED.last_error,
                last_evaluated_at_utc=EXCLUDED.last_evaluated_at_utc,
                source_outcome=EXCLUDED.source_outcome,
                status_reason=EXCLUDED.status_reason,
                updated_at_utc=now()
            WHERE auditcore.p2_control_state.last_evaluated_at_utc IS NULL
               OR EXCLUDED.last_evaluated_at_utc >= auditcore.p2_control_state.last_evaluated_at_utc;

            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key,
                payload, requested_version, work_status
            )
            SELECT
                t.tenant_id,
                t.journey_id,
                'TASK_VERIFY',
                t.task_id::text,
                jsonb_build_object(
                    'taskId', t.task_id::text,
                    'controlCode', NEW.rule_code
                ),
                v_fact_version,
                'PENDING'
            FROM auditcore.p2_tasks t
            WHERE t.tenant_id=NEW.tenant_id
              AND t.journey_id=NEW.journey_id
              AND t.task_status='VERIFYING'
              AND t.source_type='RULE'
              AND t.source_code=NEW.rule_code
            ON CONFLICT (tenant_id, work_type, work_key)
            DO UPDATE SET
                payload=EXCLUDED.payload,
                requested_version=GREATEST(
                    COALESCE(auditcore.p2_work_queue.requested_version,0),
                    COALESCE(EXCLUDED.requested_version,0)
                ),
                work_status=CASE
                    WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                      THEN auditcore.p2_work_queue.work_status
                    ELSE 'PENDING'
                END,
                next_attempt_at_utc=CASE
                    WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                      THEN auditcore.p2_work_queue.next_attempt_at_utc
                    ELSE NULL
                END,
                updated_at_utc=now();

            RETURN NEW;
        END;
        $$;

        DROP TRIGGER IF EXISTS trg_p2_rule_execution_mirror
          ON auditcore.rule_executions;
        CREATE TRIGGER trg_p2_rule_execution_mirror
          AFTER INSERT ON auditcore.rule_executions
          FOR EACH ROW EXECUTE FUNCTION auditcore.p2_mirror_rule_execution();

        CREATE OR REPLACE FUNCTION auditcore.p2_requeue_verifying_controls(
            p_tenant_id varchar,
            p_journey_id uuid,
            p_fact_version bigint
        )
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key,
                payload, requested_version, work_status
            )
            SELECT DISTINCT
                t.tenant_id,
                t.journey_id,
                'CONTROL_EVALUATE',
                t.journey_id::text || ':' || t.source_code,
                jsonb_build_object(
                    'controlCode', t.source_code,
                    'reason', 'FACT_CHANGED'
                ),
                p_fact_version,
                'PENDING'
            FROM auditcore.p2_tasks t
            WHERE t.tenant_id=p_tenant_id
              AND t.journey_id=p_journey_id
              AND t.task_status='VERIFYING'
              AND t.source_type='RULE'
              AND t.source_code IS NOT NULL
            ON CONFLICT (tenant_id, work_type, work_key)
            DO UPDATE SET
                payload=EXCLUDED.payload,
                requested_version=GREATEST(
                    COALESCE(auditcore.p2_work_queue.requested_version,0),
                    COALESCE(EXCLUDED.requested_version,0)
                ),
                work_status=CASE
                    WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                      THEN auditcore.p2_work_queue.work_status
                    ELSE 'PENDING'
                END,
                next_attempt_at_utc=CASE
                    WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                      THEN auditcore.p2_work_queue.next_attempt_at_utc
                    ELSE NULL
                END,
                updated_at_utc=now();
        END;
        $$;

        CREATE OR REPLACE FUNCTION auditcore.p2_request_stage_recompute(
            p_tenant_id varchar,
            p_journey_id uuid
        )
        RETURNS bigint
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_version bigint;
        BEGIN
            INSERT INTO auditcore.p2_journey_runtime (
                tenant_id, journey_id, fact_version
            ) VALUES (
                p_tenant_id, p_journey_id, 1
            )
            ON CONFLICT (tenant_id, journey_id)
            DO UPDATE SET fact_version = auditcore.p2_journey_runtime.fact_version + 1,
                          updated_at_utc = now()
            RETURNING fact_version INTO v_version;

            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key,
                payload, requested_version, work_status
            ) VALUES (
                p_tenant_id, p_journey_id, 'STAGE_RECOMPUTE',
                'booking:' || p_journey_id::text,
                jsonb_build_object('stage', 'BOOKING'),
                v_version,
                'PENDING'
            )
            ON CONFLICT (tenant_id, work_type, work_key)
            DO UPDATE SET requested_version = GREATEST(
                              COALESCE(auditcore.p2_work_queue.requested_version, 0),
                              EXCLUDED.requested_version
                          ),
                          payload = EXCLUDED.payload,
                          work_status = CASE
                              WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.work_status
                              ELSE 'PENDING'
                          END,
                          next_attempt_at_utc = CASE
                              WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.next_attempt_at_utc
                              ELSE NULL
                          END,
                          last_error = CASE
                              WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.last_error
                              ELSE NULL
                          END,
                          updated_at_utc = now();

            PERFORM auditcore.p2_requeue_verifying_controls(
                p_tenant_id, p_journey_id, v_version
            );

            RETURN v_version;
        END;
        $$;

        GRANT EXECUTE ON FUNCTION auditcore.p2_requeue_verifying_controls(varchar, uuid, bigint)
          TO audit_core_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        DROP TRIGGER IF EXISTS trg_p2_rule_execution_mirror ON auditcore.rule_executions;
        DROP FUNCTION IF EXISTS auditcore.p2_mirror_rule_execution();

        CREATE OR REPLACE FUNCTION auditcore.p2_request_stage_recompute(
            p_tenant_id varchar,
            p_journey_id uuid
        )
        RETURNS bigint
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_version bigint;
        BEGIN
            INSERT INTO auditcore.p2_journey_runtime (
                tenant_id, journey_id, fact_version
            ) VALUES (
                p_tenant_id, p_journey_id, 1
            )
            ON CONFLICT (tenant_id, journey_id)
            DO UPDATE SET fact_version = auditcore.p2_journey_runtime.fact_version + 1,
                          updated_at_utc = now()
            RETURNING fact_version INTO v_version;

            INSERT INTO auditcore.p2_work_queue (
                tenant_id, journey_id, work_type, work_key,
                payload, requested_version, work_status
            ) VALUES (
                p_tenant_id, p_journey_id, 'STAGE_RECOMPUTE',
                'booking:' || p_journey_id::text,
                jsonb_build_object('stage', 'BOOKING'),
                v_version,
                'PENDING'
            )
            ON CONFLICT (tenant_id, work_type, work_key)
            DO UPDATE SET requested_version = GREATEST(
                              COALESCE(auditcore.p2_work_queue.requested_version, 0),
                              EXCLUDED.requested_version
                          ),
                          payload = EXCLUDED.payload,
                          work_status = CASE
                              WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.work_status
                              ELSE 'PENDING'
                          END,
                          next_attempt_at_utc = CASE
                              WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.next_attempt_at_utc
                              ELSE NULL
                          END,
                          last_error = CASE
                              WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                                  THEN auditcore.p2_work_queue.last_error
                              ELSE NULL
                          END,
                          updated_at_utc = now();

            RETURN v_version;
        END;
        $$;

        DROP FUNCTION IF EXISTS auditcore.p2_requeue_verifying_controls(varchar, uuid, bigint);

        ALTER TABLE auditcore.p2_control_state
          DROP COLUMN IF EXISTS status_reason,
          DROP COLUMN IF EXISTS source_outcome;
        """
    )
