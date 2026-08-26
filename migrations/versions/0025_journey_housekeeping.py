"""Add privileged Journey housekeeping delete function.

Revision ID: 0025_journey_housekeeping
Revises: 0024_uc03_booking_refs
Create Date: 2026-08-26
"""
from alembic import op

revision = "0025_journey_housekeeping"
down_revision = "0024_uc03_booking_refs"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # Journey housekeeping is a SuperAdmin control-plane operation. Keep DELETE
    # privilege encapsulated in one SECURITY DEFINER function rather than granting
    # destructive rights across Audit Core transaction/audit tables to runtime.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.hard_delete_journey_transactions(
            p_tenant_id varchar,
            p_journey_ids uuid[]
        ) RETURNS jsonb
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, auditcore
        AS $$
        DECLARE
            v_requested_count integer := COALESCE(cardinality(p_journey_ids), 0);
            v_existing_count integer := 0;
            v_deleted_journeys integer := 0;
            v_deleted_customers integer := 0;
            v_deleted_evidence integer := 0;
            v_customer_ids uuid[] := ARRAY[]::uuid[];
            v_orphan_customer_ids uuid[] := ARRAY[]::uuid[];
        BEGIN
            IF p_tenant_id IS NULL OR btrim(p_tenant_id) = '' THEN
                RAISE EXCEPTION 'TENANT_ID_REQUIRED' USING ERRCODE='invalid_parameter_value';
            END IF;
            IF v_requested_count = 0 THEN
                RETURN jsonb_build_object(
                    'tenantId', p_tenant_id,
                    'deletedJourneys', 0,
                    'deletedCustomers', 0,
                    'deletedEvidence', 0
                );
            END IF;

            SELECT count(*)
            INTO v_existing_count
            FROM auditcore.journeys
            WHERE tenant_id=p_tenant_id
              AND journey_id = ANY(p_journey_ids);

            IF v_existing_count <> v_requested_count THEN
                RAISE EXCEPTION 'JOURNEY_SCOPE_MISMATCH:%/%', v_existing_count, v_requested_count
                    USING ERRCODE='check_violation';
            END IF;

            SELECT COALESCE(array_agg(DISTINCT customer_id), ARRAY[]::uuid[])
            INTO v_customer_ids
            FROM auditcore.journeys
            WHERE tenant_id=p_tenant_id
              AND journey_id = ANY(p_journey_ids);

            SELECT count(*) INTO v_deleted_evidence
            FROM auditcore.evidence
            WHERE tenant_id=p_tenant_id
              AND journey_id = ANY(p_journey_ids);

            CREATE TEMP TABLE IF NOT EXISTS journey_housekeeping_entity_ids (
                entity_id text PRIMARY KEY
            ) ON COMMIT DROP;
            TRUNCATE pg_temp.journey_housekeeping_entity_ids;

            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT journey_id::text FROM auditcore.journeys
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;

            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT evidence_id::text FROM auditcore.evidence
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT audit_evaluation_id::text FROM auditcore.audit_evaluations
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT audit_finding_id::text FROM auditcore.audit_findings
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT booking_id::text FROM auditcore.bookings
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT payment_id::text FROM auditcore.payments
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT delivery_id::text FROM auditcore.deliveries
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT workflow_instance_id::text FROM auditcore.workflow_instances
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT workflow_task_id::text FROM auditcore.workflow_tasks
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT activity_record_id::text FROM auditcore.activity_records
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT commercial_line_id::text FROM auditcore.commercial_lines
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT escalation_id::text FROM auditcore.escalations
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT finance_record_id::text FROM auditcore.finance_records
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT insurance_record_id::text FROM auditcore.insurance_records
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT capture_proposal_id::text FROM auditcore.journey_capture_proposals
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT journey_document_assessment_id::text
            FROM auditcore.journey_document_assessments
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT journey_document_requirement_id::text
            FROM auditcore.journey_document_requirements
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT journey_product_id::text FROM auditcore.journey_products
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT registration_record_id::text FROM auditcore.registration_records
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT review_decision_id::text FROM auditcore.review_decisions
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT trade_in_case_id::text FROM auditcore.trade_in_cases
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;
            INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
            SELECT vehicle_record_id::text FROM auditcore.vehicle_records
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
            ON CONFLICT DO NOTHING;

            -- Append-only triggers are intentionally bypassed only inside this
            -- owner-approved SuperAdmin housekeeping function. FK triggers stay on.
            ALTER TABLE auditcore.audit_events DISABLE TRIGGER USER;
            ALTER TABLE auditcore.audit_finding_events DISABLE TRIGGER USER;
            ALTER TABLE auditcore.audit_state_events DISABLE TRIGGER USER;
            ALTER TABLE auditcore.delivery_status_history DISABLE TRIGGER USER;
            ALTER TABLE auditcore.finding_remarks DISABLE TRIGGER USER;
            ALTER TABLE auditcore.journey_workflow_events DISABLE TRIGGER USER;
            ALTER TABLE auditcore.payment_verification_events DISABLE TRIGGER USER;
            ALTER TABLE auditcore.review_decisions DISABLE TRIGGER USER;
            ALTER TABLE auditcore.workflow_task_events DISABLE TRIGGER USER;

            DELETE FROM auditcore.audit_events a
            USING pg_temp.journey_housekeeping_entity_ids e
            WHERE a.tenant_id=p_tenant_id AND a.entity_id=e.entity_id;
            DELETE FROM auditcore.audit_chain_heads h
            USING pg_temp.journey_housekeeping_entity_ids e
            WHERE h.tenant_id=p_tenant_id AND h.entity_id=e.entity_id;

            DELETE FROM auditcore.inbox_events
            WHERE tenant_id=p_tenant_id
              AND COALESCE(event_payload->>'journeyId', event_payload->>'journey_id')
                    = ANY(ARRAY(SELECT journey_id::text FROM unnest(p_journey_ids) journey_id));
            DELETE FROM auditcore.idempotency_records
            WHERE tenant_id=p_tenant_id
              AND (
                    COALESCE(response_body->>'journeyId', response_body->>'journey_id')
                      = ANY(ARRAY(SELECT journey_id::text FROM unnest(p_journey_ids) journey_id))
                    OR logical_result_id IN (
                        SELECT entity_id FROM pg_temp.journey_housekeeping_entity_ids
                    )
              );

            UPDATE auditcore.customers
            SET legal_name_source_evidence_id=NULL,
                updated_at_utc=now()
            WHERE tenant_id=p_tenant_id
              AND legal_name_source_evidence_id IN (
                    SELECT evidence_id FROM auditcore.evidence
                    WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
              );
            UPDATE auditcore.evidence
            SET supersedes_evidence_id=NULL
            WHERE tenant_id=p_tenant_id
              AND supersedes_evidence_id IN (
                    SELECT evidence_id FROM auditcore.evidence
                    WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
              );

            DELETE FROM auditcore.workflow_task_attempts
            WHERE tenant_id=p_tenant_id
              AND workflow_task_id IN (
                    SELECT workflow_task_id FROM auditcore.workflow_tasks
                    WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
              );
            DELETE FROM auditcore.crm_interactions
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.workflow_dead_letters
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.workflow_task_events
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.workflow_tasks
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.workflow_instances
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);

            DELETE FROM auditcore.finding_evidence
            WHERE tenant_id=p_tenant_id
              AND (
                    audit_finding_id IN (
                        SELECT audit_finding_id FROM auditcore.audit_findings
                        WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
                    )
                    OR evidence_id IN (
                        SELECT evidence_id FROM auditcore.evidence
                        WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
                    )
              );
            DELETE FROM auditcore.finding_remarks
            WHERE tenant_id=p_tenant_id
              AND audit_finding_id IN (
                    SELECT audit_finding_id FROM auditcore.audit_findings
                    WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids)
              );
            DELETE FROM auditcore.audit_finding_events
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.audit_findings
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.audit_evaluations
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);

            DELETE FROM auditcore.payment_verification_events
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.delivery_status_history
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);

            DELETE FROM auditcore.daily_ops_items
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.discount_applications
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.evidence_facts
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.evidence_ingestion_operations
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.commercial_lines
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.finance_records
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.insurance_records
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.journey_capture_proposals
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.journey_delivery_audit_facts
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.journey_document_assessments
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.payments
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.registration_records
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.trade_in_cases
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.vehicle_records
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.deliveries
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);

            DELETE FROM auditcore.evidence
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.journey_document_requirements
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);

            DELETE FROM auditcore.activity_records
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.audit_state_events
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.bookings
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.escalations
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.journey_addons
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.journey_products
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.journey_stage_states
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.journey_workflow_events
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.outbox_events
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            DELETE FROM auditcore.review_decisions
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);

            DELETE FROM auditcore.journeys
            WHERE tenant_id=p_tenant_id AND journey_id = ANY(p_journey_ids);
            GET DIAGNOSTICS v_deleted_journeys = ROW_COUNT;

            IF cardinality(v_customer_ids) > 0 THEN
                SELECT COALESCE(array_agg(customer_id), ARRAY[]::uuid[])
                INTO v_orphan_customer_ids
                FROM auditcore.customers c
                WHERE c.tenant_id=p_tenant_id
                  AND c.customer_id = ANY(v_customer_ids)
                  AND NOT EXISTS (
                        SELECT 1 FROM auditcore.journeys j
                        WHERE j.tenant_id=c.tenant_id AND j.customer_id=c.customer_id
                  );

                IF cardinality(v_orphan_customer_ids) > 0 THEN
                    INSERT INTO pg_temp.journey_housekeeping_entity_ids(entity_id)
                    SELECT customer_id::text FROM unnest(v_orphan_customer_ids) customer_id
                    ON CONFLICT DO NOTHING;

                    DELETE FROM auditcore.audit_events a
                    USING pg_temp.journey_housekeeping_entity_ids e
                    WHERE a.tenant_id=p_tenant_id AND a.entity_id=e.entity_id;
                    DELETE FROM auditcore.audit_chain_heads h
                    USING pg_temp.journey_housekeeping_entity_ids e
                    WHERE h.tenant_id=p_tenant_id AND h.entity_id=e.entity_id;
                    DELETE FROM auditcore.idempotency_records
                    WHERE tenant_id=p_tenant_id
                      AND logical_result_id = ANY(
                            ARRAY(SELECT customer_id::text FROM unnest(v_orphan_customer_ids) customer_id)
                      );
                    DELETE FROM auditcore.di_subject_mappings
                    WHERE tenant_id=p_tenant_id
                      AND customer_id = ANY(v_orphan_customer_ids);
                    DELETE FROM auditcore.customer_identity_index
                    WHERE tenant_id=p_tenant_id
                      AND customer_id = ANY(v_orphan_customer_ids);
                    DELETE FROM auditcore.customers
                    WHERE tenant_id=p_tenant_id
                      AND customer_id = ANY(v_orphan_customer_ids);
                    GET DIAGNOSTICS v_deleted_customers = ROW_COUNT;
                END IF;
            END IF;

            ALTER TABLE auditcore.audit_events ENABLE TRIGGER USER;
            ALTER TABLE auditcore.audit_finding_events ENABLE TRIGGER USER;
            ALTER TABLE auditcore.audit_state_events ENABLE TRIGGER USER;
            ALTER TABLE auditcore.delivery_status_history ENABLE TRIGGER USER;
            ALTER TABLE auditcore.finding_remarks ENABLE TRIGGER USER;
            ALTER TABLE auditcore.journey_workflow_events ENABLE TRIGGER USER;
            ALTER TABLE auditcore.payment_verification_events ENABLE TRIGGER USER;
            ALTER TABLE auditcore.review_decisions ENABLE TRIGGER USER;
            ALTER TABLE auditcore.workflow_task_events ENABLE TRIGGER USER;

            RETURN jsonb_build_object(
                'tenantId', p_tenant_id,
                'deletedJourneys', v_deleted_journeys,
                'deletedCustomers', v_deleted_customers,
                'deletedEvidence', v_deleted_evidence
            );
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "auditcore.hard_delete_journey_transactions(varchar, uuid[]) FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION "
        f"auditcore.hard_delete_journey_transactions(varchar, uuid[]) TO {_RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "auditcore.hard_delete_journey_transactions(varchar, uuid[])"
    )
