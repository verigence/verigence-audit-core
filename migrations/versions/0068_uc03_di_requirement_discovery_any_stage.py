"""Widen the DI requirement-discovery RLS policy to Booking and Delivery.

Revision ID: 0068_uc03_di_discovery_any_stage
Revises: 0067_uc03_automated_sync_failure
Create Date: 2026-09-08

The DI -> Audit Core document-link callback (``/v1/internal/di/booking-
document-links``) is not actually Booking-specific -- DI has no notion of
process area, it just reports "this document is linked to this requirement".
The 0027 RLS policy backing that callback's pre-tenant-context discovery
step hardcoded ``process_area = 'BOOKING'``, so a Delivery document's link
silently found zero rows, was never acknowledged, and DI retried it forever.

This widens the policy to ``process_area IN ('BOOKING','DELIVERY')``. Which
requirement (and therefore which stage) a given callback resolves to remains
entirely data-driven -- the requirement row itself, never the policy or the
endpoint.
"""
from alembic import op

revision = "0068_uc03_di_discovery_any_stage"
down_revision = "0067_uc03_automated_sync_failure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS internal_di_booking_requirement_discovery "
        "ON auditcore.journey_document_requirements"
    )
    op.execute(
        """
        CREATE POLICY internal_di_booking_requirement_discovery
        ON auditcore.journey_document_requirements
        FOR SELECT
        USING (
            auditcore.current_tenant_id() IS NULL
            AND auditcore.current_internal_service_id() IS NOT NULL
            AND auditcore.current_di_requirement_ref() IS NOT NULL
            AND upper(process_area) IN ('BOOKING', 'DELIVERY')
            AND journey_document_requirement_id::text = auditcore.current_di_requirement_ref()
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS internal_di_booking_requirement_discovery "
        "ON auditcore.journey_document_requirements"
    )
    op.execute(
        """
        CREATE POLICY internal_di_booking_requirement_discovery
        ON auditcore.journey_document_requirements
        FOR SELECT
        USING (
            auditcore.current_tenant_id() IS NULL
            AND auditcore.current_internal_service_id() IS NOT NULL
            AND auditcore.current_di_requirement_ref() IS NOT NULL
            AND upper(process_area) = 'BOOKING'
            AND journey_document_requirement_id::text = auditcore.current_di_requirement_ref()
        )
        """
    )
