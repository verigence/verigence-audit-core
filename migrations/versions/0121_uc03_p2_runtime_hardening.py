"""0121_uc03_p2_runtime_hardening — reliable P2 worker runtime.

1. Remove the P2 row triggers from shared legacy write paths (evidence,
   journey_document_extracted_fields, payments, workflow_tasks). Each fired
   once per written row and upserted one shared p2_journey_runtime row, so a
   single document sync performed dozens of writes to that row and every
   concurrent sync on the same Journey serialized behind it. P2 now requests
   recomputation explicitly from its own code paths and the worker runs a
   fingerprint sweep as a safety net (uc03_p2_worker._fact_sweep).
2. Lease ownership for p2_work_queue: every claim writes a unique lease token
   and every completion/failure/reschedule is conditional on it, so a worker
   whose lease expired can never overwrite the result of the worker that
   reclaimed the item.
3. Journey-level DI reconciliation (JOURNEY_RECONCILE): one DI listing per
   Journey poll instead of one per page.
4. Durable DI status on each page so the worker can distinguish
   "still processing" from terminal outcomes and never polls forever.
5. Indexes for the sweep and the hot P2 read paths.
"""
from __future__ import annotations

from alembic import op

revision = "0121_uc03_p2_runtime_hardening"
down_revision = "0120_uc03_p2_doc_replace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        DROP TRIGGER IF EXISTS trg_p2_evidence_change ON auditcore.evidence;
        DROP TRIGGER IF EXISTS trg_p2_extracted_fact_change
          ON auditcore.journey_document_extracted_fields;
        DROP TRIGGER IF EXISTS trg_p2_payment_change ON auditcore.payments;
        DROP TRIGGER IF EXISTS trg_p2_legacy_manual_task_change
          ON auditcore.workflow_tasks;
        DROP FUNCTION IF EXISTS auditcore.p2_fact_change_trigger();

        ALTER TABLE auditcore.p2_work_queue
          ADD COLUMN lease_token uuid,
          ADD COLUMN claim_count integer NOT NULL DEFAULT 0 CHECK (claim_count >= 0),
          DROP CONSTRAINT IF EXISTS p2_work_queue_work_type_check,
          ADD CONSTRAINT p2_work_queue_work_type_check CHECK (work_type IN (
            'SPLIT_BATCH','DOCUMENT_INGEST','DOCUMENT_RECONCILE','JOURNEY_RECONCILE',
            'STAGE_RECOMPUTE','CONTROL_EVALUATE','TASK_VERIFY'
          ));

        CREATE INDEX IF NOT EXISTS ix_p2_work_queue_tenant_claim
          ON auditcore.p2_work_queue(tenant_id, work_status, next_attempt_at_utc, created_at_utc);

        ALTER TABLE auditcore.p2_document_queue
          ADD COLUMN di_state varchar(30),
          ADD COLUMN di_processing_status varchar(30),
          ADD COLUMN di_submitted_at_utc timestamptz,
          ADD COLUMN di_processed_seen_at_utc timestamptz,
          ADD COLUMN status_reason text;

        CREATE INDEX IF NOT EXISTS ix_p2_document_queue_di_document
          ON auditcore.p2_document_queue(tenant_id, di_document_id)
          WHERE di_document_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_p2_document_queue_journey_status
          ON auditcore.p2_document_queue(tenant_id, journey_id, queue_status);

        ALTER TABLE auditcore.p2_journey_runtime
          ADD COLUMN fact_fingerprint varchar(64);

        CREATE INDEX IF NOT EXISTS ix_p2_tasks_journey
          ON auditcore.p2_tasks(tenant_id, journey_id, task_status);
        CREATE INDEX IF NOT EXISTS ix_p2_tasks_document_ref
          ON auditcore.p2_tasks(tenant_id, journey_id, ((reference->>'documentId')));

        CREATE INDEX IF NOT EXISTS ix_extracted_fields_tenant_updated
          ON auditcore.journey_document_extracted_fields(tenant_id, updated_at_utc);
        CREATE INDEX IF NOT EXISTS ix_payments_tenant_updated
          ON auditcore.payments(tenant_id, updated_at_utc);
        CREATE INDEX IF NOT EXISTS ix_workflow_tasks_tenant_updated
          ON auditcore.workflow_tasks(tenant_id, updated_at_utc);
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        DROP INDEX IF EXISTS auditcore.ix_workflow_tasks_tenant_updated;
        DROP INDEX IF EXISTS auditcore.ix_payments_tenant_updated;
        DROP INDEX IF EXISTS auditcore.ix_extracted_fields_tenant_updated;
        DROP INDEX IF EXISTS auditcore.ix_p2_tasks_document_ref;
        DROP INDEX IF EXISTS auditcore.ix_p2_tasks_journey;
        ALTER TABLE auditcore.p2_journey_runtime DROP COLUMN IF EXISTS fact_fingerprint;
        DROP INDEX IF EXISTS auditcore.ix_p2_document_queue_journey_status;
        DROP INDEX IF EXISTS auditcore.ix_p2_document_queue_di_document;
        ALTER TABLE auditcore.p2_document_queue
          DROP COLUMN IF EXISTS status_reason,
          DROP COLUMN IF EXISTS di_processed_seen_at_utc,
          DROP COLUMN IF EXISTS di_submitted_at_utc,
          DROP COLUMN IF EXISTS di_processing_status,
          DROP COLUMN IF EXISTS di_state;
        DROP INDEX IF EXISTS auditcore.ix_p2_work_queue_tenant_claim;
        DELETE FROM auditcore.p2_work_queue WHERE work_type='JOURNEY_RECONCILE';
        ALTER TABLE auditcore.p2_work_queue
          DROP CONSTRAINT IF EXISTS p2_work_queue_work_type_check,
          ADD CONSTRAINT p2_work_queue_work_type_check CHECK (work_type IN (
            'SPLIT_BATCH','DOCUMENT_INGEST','DOCUMENT_RECONCILE',
            'STAGE_RECOMPUTE','CONTROL_EVALUATE','TASK_VERIFY'
          )),
          DROP COLUMN IF EXISTS claim_count,
          DROP COLUMN IF EXISTS lease_token;

        CREATE OR REPLACE FUNCTION auditcore.p2_fact_change_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM auditcore.p2_request_stage_recompute(NEW.tenant_id, NEW.journey_id);
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_p2_evidence_change
          AFTER INSERT OR UPDATE OF association_status, document_type_key, process_area
          ON auditcore.evidence
          FOR EACH ROW EXECUTE FUNCTION auditcore.p2_fact_change_trigger();
        CREATE TRIGGER trg_p2_extracted_fact_change
          AFTER INSERT OR UPDATE OF effective_value, modified_value, source_document_type_key
          ON auditcore.journey_document_extracted_fields
          FOR EACH ROW EXECUTE FUNCTION auditcore.p2_fact_change_trigger();
        CREATE TRIGGER trg_p2_payment_change
          AFTER INSERT OR UPDATE OF amount, receipt_date, receipt_number, payment_stage
          ON auditcore.payments
          FOR EACH ROW EXECUTE FUNCTION auditcore.p2_fact_change_trigger();
        CREATE TRIGGER trg_p2_legacy_manual_task_change
          AFTER INSERT OR UPDATE OF task_status
          ON auditcore.workflow_tasks
          FOR EACH ROW EXECUTE FUNCTION auditcore.p2_manual_task_change_trigger();
        """
    )
