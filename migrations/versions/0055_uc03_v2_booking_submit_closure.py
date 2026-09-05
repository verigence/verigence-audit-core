"""Align submitted UC03 V2 Booking business state with the approved workflow.

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-05

V2 Submit Booking is allowed only after the active mandatory document set is
satisfied and all mandatory Booking Details validate. Historical V2 submissions
were nevertheless left as BOOKING_IN_PROGRESS. Repair only rows proven to have
come through the V2 single-submit path; PC verification remains an independent
audit state and may still be PENDING.
"""

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE auditcore.journey_stage_states AS s
        SET business_status = 'BOOKING_CLOSED',
            closure_disposition = 'PROCEED_TO_DELIVERY',
            business_completed_at_utc = COALESCE(
                s.business_completed_at_utc,
                s.capture_completed_at_utc
            ),
            closed_at_utc = COALESCE(s.closed_at_utc, s.capture_completed_at_utc),
            closed_by_actor_id = COALESCE(
                s.closed_by_actor_id,
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
                )
            ),
            updated_at_utc = now()
        WHERE s.stage_code = 'BOOKING'
          AND s.capture_completed_at_utc IS NOT NULL
          AND s.business_status IN ('BOOKING_STARTED', 'BOOKING_IN_PROGRESS')
          AND EXISTS (
                SELECT 1
                FROM auditcore.journey_workflow_events e
                WHERE e.tenant_id = s.tenant_id
                  AND e.journey_id = s.journey_id
                  AND e.stage_code = 'BOOKING'
                  AND e.event_type = 'PC_BOOKING_CAPTURE_SUBMITTED'
                  AND e.safe_payload->>'capturePath' = 'V2_SINGLE_SUBMIT'
          )
        """
    )


def downgrade() -> None:
    # This is a correction of persisted business facts. Reverting it would
    # incorrectly reopen Bookings that have already been submitted.
    pass
