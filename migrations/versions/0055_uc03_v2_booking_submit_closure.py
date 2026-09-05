"""Align UC03 V2 Booking status with mandatory-document completeness.

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-05

Missing documents remain non-blocking for the PC capture journey. Booking business
completion is simpler and independent: a V2 Booking is complete only when every
mandatory non-identity Booking requirement has a CLASSIFIED linked document and the
existing PAN/Aadhaar one-of identity requirement is satisfied. Historical V2
submissions are repaired from the evidence already persisted in Audit Core; no DI
call is made by this migration.
"""

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        WITH v2_submitted AS (
            SELECT
                s.tenant_id,
                s.journey_id,
                (
                    SELECT e.actor_id
                    FROM auditcore.journey_workflow_events e
                    WHERE e.tenant_id = s.tenant_id
                      AND e.journey_id = s.journey_id
                      AND e.stage_code = 'BOOKING'
                      AND e.event_type = 'PC_BOOKING_CAPTURE_SUBMITTED'
                      AND e.safe_payload->>'capturePath' = 'V2_SINGLE_SUBMIT'
                    ORDER BY e.occurred_at_utc DESC, e.recorded_at_utc DESC
                    LIMIT 1
                ) AS submitted_by_actor_id,
                (
                    SELECT e.occurred_at_utc
                    FROM auditcore.journey_workflow_events e
                    WHERE e.tenant_id = s.tenant_id
                      AND e.journey_id = s.journey_id
                      AND e.stage_code = 'BOOKING'
                      AND e.event_type = 'PC_BOOKING_CAPTURE_SUBMITTED'
                      AND e.safe_payload->>'capturePath' = 'V2_SINGLE_SUBMIT'
                    ORDER BY e.occurred_at_utc DESC, e.recorded_at_utc DESC
                    LIMIT 1
                ) AS submitted_at_utc
            FROM auditcore.journey_stage_states s
            WHERE s.stage_code = 'BOOKING'
              AND s.business_status IN (
                    'BOOKING_STARTED', 'BOOKING_IN_PROGRESS', 'BOOKING_CLOSED'
              )
              AND EXISTS (
                    SELECT 1
                    FROM auditcore.journey_workflow_events e
                    WHERE e.tenant_id = s.tenant_id
                      AND e.journey_id = s.journey_id
                      AND e.stage_code = 'BOOKING'
                      AND e.event_type = 'PC_BOOKING_CAPTURE_SUBMITTED'
                      AND e.safe_payload->>'capturePath' = 'V2_SINGLE_SUBMIT'
              )
        ),
        mandatory_state AS (
            SELECT
                v.tenant_id,
                v.journey_id,
                v.submitted_by_actor_id,
                v.submitted_at_utc,
                NOT EXISTS (
                    SELECT 1
                    FROM auditcore.journey_document_requirements r
                    WHERE r.tenant_id = v.tenant_id
                      AND r.journey_id = v.journey_id
                      AND upper(r.process_area) = 'BOOKING'
                      AND upper(r.requirement_level) = 'REQUIRED'
                      AND upper(
                            coalesce(r.requirement_key, '') || ' ' ||
                            coalesce(r.document_type_key, '')
                          ) !~ '(PAN|AADHAAR|AADHAR)'
                      AND NOT EXISTS (
                            SELECT 1
                            FROM auditcore.document_capture_v2_documents d
                            WHERE d.tenant_id = r.tenant_id
                              AND d.journey_id = r.journey_id
                              AND d.stage_code = 'BOOKING'
                              AND d.capture_status = 'CLASSIFIED'
                              AND d.requirement_key = r.requirement_key
                      )
                ) AS non_identity_complete,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM auditcore.journey_document_requirements r
                        WHERE r.tenant_id = v.tenant_id
                          AND r.journey_id = v.journey_id
                          AND upper(r.process_area) = 'BOOKING'
                          AND upper(r.requirement_level) = 'REQUIRED'
                          AND upper(
                                coalesce(r.requirement_key, '') || ' ' ||
                                coalesce(r.document_type_key, '')
                              ) ~ '(PAN|AADHAAR|AADHAR)'
                    )
                    THEN EXISTS (
                        SELECT 1
                        FROM auditcore.journey_document_requirements r
                        JOIN auditcore.document_capture_v2_documents d
                          ON d.tenant_id = r.tenant_id
                         AND d.journey_id = r.journey_id
                         AND d.stage_code = 'BOOKING'
                         AND d.capture_status = 'CLASSIFIED'
                         AND d.requirement_key = r.requirement_key
                        WHERE r.tenant_id = v.tenant_id
                          AND r.journey_id = v.journey_id
                          AND upper(r.process_area) = 'BOOKING'
                          AND upper(r.requirement_level) = 'REQUIRED'
                          AND upper(
                                coalesce(r.requirement_key, '') || ' ' ||
                                coalesce(r.document_type_key, '')
                              ) ~ '(PAN|AADHAAR|AADHAR)'
                    )
                    ELSE true
                END AS identity_complete
            FROM v2_submitted v
        )
        UPDATE auditcore.journey_stage_states AS s
        SET business_status = CASE
                WHEN m.non_identity_complete AND m.identity_complete
                    THEN 'BOOKING_CLOSED'
                ELSE 'BOOKING_IN_PROGRESS'
            END,
            closure_disposition = CASE
                WHEN m.non_identity_complete AND m.identity_complete
                    THEN 'PROCEED_TO_DELIVERY'
                ELSE NULL
            END,
            capture_completed_at_utc = CASE
                WHEN m.non_identity_complete AND m.identity_complete
                    THEN COALESCE(s.capture_completed_at_utc, m.submitted_at_utc)
                ELSE NULL
            END,
            pc_verification_status = CASE
                WHEN m.non_identity_complete AND m.identity_complete
                    THEN COALESCE(s.pc_verification_status, 'PENDING')
                WHEN s.pc_verification_status = 'PENDING'
                    THEN NULL
                ELSE s.pc_verification_status
            END,
            business_completed_at_utc = CASE
                WHEN m.non_identity_complete AND m.identity_complete
                    THEN COALESCE(
                        s.business_completed_at_utc,
                        s.capture_completed_at_utc,
                        m.submitted_at_utc
                    )
                ELSE NULL
            END,
            closed_at_utc = CASE
                WHEN m.non_identity_complete AND m.identity_complete
                    THEN COALESCE(
                        s.closed_at_utc,
                        s.capture_completed_at_utc,
                        m.submitted_at_utc
                    )
                ELSE NULL
            END,
            closed_by_actor_id = CASE
                WHEN m.non_identity_complete AND m.identity_complete
                    THEN COALESCE(s.closed_by_actor_id, m.submitted_by_actor_id)
                ELSE NULL
            END,
            updated_at_utc = now()
        FROM mandatory_state m
        WHERE s.tenant_id = m.tenant_id
          AND s.journey_id = m.journey_id
          AND s.stage_code = 'BOOKING'
        """
    )


def downgrade() -> None:
    # This migration reconciles historical business state from persisted evidence.
    # Reversing it would deliberately restore states known to be inconsistent.
    pass
