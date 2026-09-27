"""0115_uc03_p2_event_triggers — event-driven P2 stage recomputation.

Existing DI/Audit Core writers are left untouched. Durable DB triggers observe
the facts P2 Booking completion depends on and coalesce a versioned
STAGE_RECOMPUTE work item. If facts change while a reducer is running,
requested_version advances; worker completion requeues instead of losing the
newer change.
"""
from alembic import op

revision = "0115_uc03_p2_event_triggers"
down_revision = "0114_uc03_p2_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
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

        CREATE OR REPLACE FUNCTION auditcore.p2_fact_change_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_tenant_id varchar;
            v_journey_id uuid;
        BEGIN
            v_tenant_id := NEW.tenant_id;
            v_journey_id := NEW.journey_id;
            PERFORM auditcore.p2_request_stage_recompute(v_tenant_id, v_journey_id);
            RETURN NEW;
        END;
        $$;

        CREATE OR REPLACE FUNCTION auditcore.p2_manual_task_change_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.task_type <> 'MANUAL_VERIFICATION_REVIEW' THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND NEW.task_status IS NOT DISTINCT FROM OLD.task_status THEN
                RETURN NEW;
            END IF;
            PERFORM auditcore.p2_request_stage_recompute(
                NEW.tenant_id,
                NEW.journey_id
            );
            RETURN NEW;
        END;
        $$;

        DROP TRIGGER IF EXISTS trg_p2_extracted_fact_change
          ON auditcore.journey_document_extracted_fields;
        CREATE TRIGGER trg_p2_extracted_fact_change
          AFTER INSERT OR UPDATE OF effective_value, modified_value, source_document_type_key
          ON auditcore.journey_document_extracted_fields
          FOR EACH ROW EXECUTE FUNCTION auditcore.p2_fact_change_trigger();

        DROP TRIGGER IF EXISTS trg_p2_payment_change
          ON auditcore.payments;
        CREATE TRIGGER trg_p2_payment_change
          AFTER INSERT OR UPDATE OF amount, receipt_date, receipt_number, payment_stage
          ON auditcore.payments
          FOR EACH ROW EXECUTE FUNCTION auditcore.p2_fact_change_trigger();

        DROP TRIGGER IF EXISTS trg_p2_legacy_manual_task_change
          ON auditcore.workflow_tasks;
        CREATE TRIGGER trg_p2_legacy_manual_task_change
          AFTER INSERT OR UPDATE OF task_status
          ON auditcore.workflow_tasks
          FOR EACH ROW EXECUTE FUNCTION auditcore.p2_manual_task_change_trigger();

        DROP TRIGGER IF EXISTS trg_p2_manual_task_change
          ON auditcore.p2_tasks;
        CREATE TRIGGER trg_p2_manual_task_change
          AFTER INSERT OR UPDATE OF task_status
          ON auditcore.p2_tasks
          FOR EACH ROW EXECUTE FUNCTION auditcore.p2_manual_task_change_trigger();

        GRANT EXECUTE ON FUNCTION auditcore.p2_request_stage_recompute(varchar, uuid)
          TO audit_core_runtime;

        INSERT INTO auditcore.p2_journey_runtime (
            tenant_id, journey_id, fact_version
        )
        SELECT j.tenant_id, j.journey_id, 1
        FROM auditcore.journeys j
        ON CONFLICT (tenant_id, journey_id) DO NOTHING;

        INSERT INTO auditcore.p2_work_queue (
            tenant_id, journey_id, work_type, work_key,
            payload, requested_version, work_status
        )
        SELECT
            j.tenant_id,
            j.journey_id,
            'STAGE_RECOMPUTE',
            'booking:' || j.journey_id::text,
            jsonb_build_object('stage', 'BOOKING'),
            1,
            'PENDING'
        FROM auditcore.journeys j
        ON CONFLICT (tenant_id, work_type, work_key)
        DO UPDATE SET requested_version=GREATEST(
                          COALESCE(auditcore.p2_work_queue.requested_version,0), 1
                      ),
                      work_status=CASE
                        WHEN auditcore.p2_work_queue.work_status IN ('CLAIMED','PROCESSING')
                          THEN auditcore.p2_work_queue.work_status
                        ELSE 'PENDING'
                      END,
                      updated_at_utc=now();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_p2_manual_task_change ON auditcore.p2_tasks;
        DROP TRIGGER IF EXISTS trg_p2_legacy_manual_task_change ON auditcore.workflow_tasks;
        DROP TRIGGER IF EXISTS trg_p2_payment_change ON auditcore.payments;
        DROP TRIGGER IF EXISTS trg_p2_extracted_fact_change
          ON auditcore.journey_document_extracted_fields;
        DROP FUNCTION IF EXISTS auditcore.p2_manual_task_change_trigger();
        DROP FUNCTION IF EXISTS auditcore.p2_fact_change_trigger();
        DROP FUNCTION IF EXISTS auditcore.p2_request_stage_recompute(varchar, uuid);
        """
    )
