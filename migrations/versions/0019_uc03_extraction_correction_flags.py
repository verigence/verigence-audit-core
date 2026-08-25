"""Raise confidence-based audit Flags for UC03 extraction corrections.

The existing correction API writes the typed-domain value and marks the immutable
machine proposal CORRECTED in one database transaction. This trigger creates the
required INFO/HIGH audit finding in that same transaction, so a corrected value can
never commit without its corresponding Flag.
"""
from alembic import op

revision = "0018_uc03_correction_flags"
down_revision = "0017_uc03_identity_business_date"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.raise_uc03_extraction_correction_flag()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_flag_id uuid;
            v_event_id uuid;
            v_severity varchar(30);
            v_title varchar(300);
            v_machine_value text;
            v_corrected_value text;
            v_confidence_percent numeric(7,2);
            v_description text;
        BEGIN
            IF NEW.stage_code <> 'BOOKING'
               OR NEW.proposal_status <> 'CORRECTED'
               OR OLD.proposal_status = 'CORRECTED' THEN
                RETURN NEW;
            END IF;

            v_severity := CASE
                WHEN NEW.confidence_score >= 0.90 THEN 'HIGH'
                ELSE 'INFO'
            END;
            v_title := CASE
                WHEN NEW.confidence_score >= 0.90
                    THEN 'High-confidence DI value corrected — TL review required'
                ELSE 'DI value corrected by PC'
            END;

            v_machine_value := COALESCE(
                NEW.proposed_value ->> 'value',
                NEW.proposed_value #>> '{}',
                NEW.proposed_value::text,
                '<null>'
            );
            v_corrected_value := COALESCE(
                NEW.accepted_value ->> 'value',
                NEW.accepted_value #>> '{}',
                NEW.accepted_value::text,
                '<null>'
            );
            v_confidence_percent := round(COALESCE(NEW.confidence_score, 0) * 100, 2);
            v_description := format(
                'Field %s changed from "%s" to "%s". DI confidence: %s%%.',
                NEW.field_key,
                v_machine_value,
                v_corrected_value,
                trim(trailing '.' FROM trim(trailing '0' FROM v_confidence_percent::text))
            );

            INSERT INTO auditcore.audit_findings (
                tenant_id,
                journey_id,
                finding_type_code,
                severity,
                finding_status,
                title,
                description,
                expected_summary,
                observed_summary,
                created_by_actor_id,
                correlation_id,
                stage_code,
                origin_kind,
                origin_actor_id,
                origin_role_snapshot,
                rule_key,
                blocking_completion
            ) VALUES (
                NEW.tenant_id,
                NEW.journey_id,
                'DOCUMENT_EXCEPTION',
                v_severity,
                'OPEN',
                v_title,
                v_description,
                format('Changed from: %s', v_machine_value),
                format('Changed to: %s', v_corrected_value),
                NEW.accepted_by_actor_id,
                NULL,
                'BOOKING',
                'HUMAN',
                NEW.accepted_by_actor_id,
                NEW.accepted_by_role,
                'UC03_DI_CORRECTION_CONFIDENCE',
                false
            )
            RETURNING audit_finding_id INTO v_flag_id;

            INSERT INTO auditcore.finding_evidence (
                tenant_id,
                audit_finding_id,
                evidence_id,
                linkage_purpose
            ) VALUES (
                NEW.tenant_id,
                v_flag_id,
                NEW.source_evidence_id,
                'CORRECTION_SOURCE'
            );

            INSERT INTO auditcore.audit_finding_events (
                tenant_id,
                audit_finding_id,
                journey_id,
                stage_code,
                event_type,
                actor_id,
                actor_role_snapshot,
                safe_payload
            ) VALUES (
                NEW.tenant_id,
                v_flag_id,
                NEW.journey_id,
                'BOOKING',
                'RAISED',
                NEW.accepted_by_actor_id,
                NEW.accepted_by_role,
                jsonb_build_object(
                    'proposalId', NEW.capture_proposal_id,
                    'fieldKey', NEW.field_key,
                    'sourceEvidenceId', NEW.source_evidence_id,
                    'confidenceScore', NEW.confidence_score,
                    'severity', v_severity,
                    'thresholdPercent', 90
                )
            )
            RETURNING finding_event_id INTO v_event_id;

            UPDATE auditcore.journey_stage_states
            SET audit_state = CASE
                    WHEN audit_state = 'NOT_STARTED' THEN 'IN_PROGRESS'
                    ELSE audit_state
                END,
                audit_status = 'FLAGS_RAISED',
                latest_activity_at_utc = now(),
                updated_at_utc = now()
            WHERE tenant_id = NEW.tenant_id
              AND journey_id = NEW.journey_id
              AND stage_code = 'BOOKING';

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_uc03_extraction_correction_flag
        ON auditcore.journey_capture_proposals
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_uc03_extraction_correction_flag
        AFTER UPDATE OF proposal_status, accepted_value
        ON auditcore.journey_capture_proposals
        FOR EACH ROW
        WHEN (
            NEW.stage_code = 'BOOKING'
            AND NEW.proposal_status = 'CORRECTED'
            AND OLD.proposal_status IS DISTINCT FROM 'CORRECTED'
        )
        EXECUTE FUNCTION auditcore.raise_uc03_extraction_correction_flag()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_uc03_extraction_correction_flag
        ON auditcore.journey_capture_proposals
        """
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.raise_uc03_extraction_correction_flag()")
