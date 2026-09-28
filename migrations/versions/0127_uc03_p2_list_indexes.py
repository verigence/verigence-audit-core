"""0127_uc03_p2_list_indexes — indexes the Phase 2 journey list and Task
Queue need to stay fast on real data: journeys newest-first per tenant, and
existing (Phase 1) workflow tasks per journey (workflow_tasks had no journey
index, so any per-journey lookup scanned the tenant's whole task history).

Also the Booking's pricing date: the date whose price-list and discount-
scheme versions price the deal. NULL keeps today's behaviour (the booking
date); a reviewer can set it to the invoice date or another date, with a
reason, when the deal genuinely belongs under a later master."""
from __future__ import annotations

from alembic import op

revision = "0127_uc03_p2_list_indexes"
down_revision = "0126_uc03_p2_vehicle_photos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_p2_journeys_tenant_recent
          ON auditcore.journeys (tenant_id, updated_at_utc DESC, journey_id DESC);
        CREATE INDEX IF NOT EXISTS ix_p2_workflow_tasks_journey
          ON auditcore.workflow_tasks (tenant_id, journey_id, task_status)
          WHERE journey_id IS NOT NULL;
        ALTER TABLE auditcore.bookings
          ADD COLUMN IF NOT EXISTS pricing_effective_on date,
          ADD COLUMN IF NOT EXISTS pricing_basis varchar(16),
          ADD COLUMN IF NOT EXISTS pricing_reason text,
          ADD COLUMN IF NOT EXISTS pricing_set_by_actor_id varchar(128),
          ADD COLUMN IF NOT EXISTS pricing_set_at_utc timestamptz;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auditcore.bookings
          DROP COLUMN IF EXISTS pricing_set_at_utc,
          DROP COLUMN IF EXISTS pricing_set_by_actor_id,
          DROP COLUMN IF EXISTS pricing_reason,
          DROP COLUMN IF EXISTS pricing_basis,
          DROP COLUMN IF EXISTS pricing_effective_on;
        DROP INDEX IF EXISTS auditcore.ix_p2_workflow_tasks_journey;
        DROP INDEX IF EXISTS auditcore.ix_p2_journeys_tenant_recent;
        """
    )
