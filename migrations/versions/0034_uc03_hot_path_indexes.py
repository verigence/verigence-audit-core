"""Add indexes for UC03 landing, Work Queue, and Booking hot paths.

Revision ID: 0034_uc03_hot_indexes
Revises: 0033_uc03_project_scope
Create Date: 2026-08-27
"""
from alembic import op

revision = "0034_uc03_hot_indexes"
down_revision = "0033_uc03_project_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Actor scope resolution used by workspace discovery, Work Queue, and Booking.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_assign_actor_scope
        ON auditcore.business_assignments (
            security_actor_id, tenant_id, dealer_id, outlet_id,
            effective_from, effective_to
        )
        WHERE assignment_status = 'ACTIVE'
        """
    )

    # Candidate ranking and selected-outlet queue scans.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_journeys_outlet_activity
        ON auditcore.journeys (
            tenant_id, outlet_id, updated_at_utc DESC, journey_id DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_stage_journey_activity
        ON auditcore.journey_stage_states (
            tenant_id, journey_id, stage_code, latest_activity_at_utc DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_booking_journey_activity
        ON auditcore.bookings (tenant_id, journey_id, updated_at_utc DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_delivery_journey_activity
        ON auditcore.deliveries (tenant_id, journey_id, updated_at_utc DESC)
        """
    )

    # Work Queue enrichment is now limited to the first page, but each candidate
    # still needs quick evidence/finding/processing lookups.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_evidence_journey_activity
        ON auditcore.evidence (
            tenant_id, journey_id, association_status,
            cache_updated_at_utc DESC, linked_at_utc DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_findings_journey_activity
        ON auditcore.audit_findings (tenant_id, journey_id, updated_at_utc DESC)
        INCLUDE (finding_status, severity)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_ingestion_journey_activity
        ON auditcore.evidence_ingestion_operations (
            tenant_id, journey_id, updated_at_utc DESC
        )
        INCLUDE (operation_status)
        """
    )

    # Booking Part-1 paints exactly the Booking requirements and active Evidence.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_uc03_requirements_part1
        ON auditcore.journey_document_requirements (
            tenant_id, journey_id, process_area, requirement_key
        )
        """
    )


def downgrade() -> None:
    for index_name in (
        "ix_uc03_requirements_part1",
        "ix_uc03_ingestion_journey_activity",
        "ix_uc03_findings_journey_activity",
        "ix_uc03_evidence_journey_activity",
        "ix_uc03_delivery_journey_activity",
        "ix_uc03_booking_journey_activity",
        "ix_uc03_stage_journey_activity",
        "ix_uc03_journeys_outlet_activity",
        "ix_uc03_assign_actor_scope",
    ):
        op.execute(f"DROP INDEX IF EXISTS auditcore.{index_name}")
