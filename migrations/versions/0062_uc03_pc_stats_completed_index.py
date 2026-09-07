"""Partial index for the UC03 PC performance stats (completed counts).

Revision ID: 0062
Revises: 0061
Create Date: 2026-09-06

GET /v1/tenants/{tenant_id}/uc03/pc-stats reports how many Bookings and Deliveries
a Process Coordinator completed inside a date window, alongside the same
in-progress counters as /landing-metrics. The completed counts filter
auditcore.journey_stage_states by business_completed_at_utc within the actor's
already-scoped set of journeys. This partial index keeps that range scan cheap
and is only maintained for stages that have actually completed, so it adds no
measurable cost to the capture write path.
"""

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_stage_business_completed
        ON auditcore.journey_stage_states (
            tenant_id, stage_code, business_completed_at_utc
        )
        WHERE business_completed_at_utc IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS auditcore.ix_uc03_stage_business_completed")
