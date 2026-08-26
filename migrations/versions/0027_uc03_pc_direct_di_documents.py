"""UC03 PC Booking direct-DI document linkage support.

Revision ID: 0027_uc03_pc_direct_di_documents
Revises: 0026_journey_housekeeping_fk
Create Date: 2026-08-26
"""
from alembic import op

revision = "0027_uc03_pc_direct_di_documents"
down_revision = "0026_journey_housekeeping_fk"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "audit_core_runtime"


def upgrade() -> None:
    # The DI linkage callback intentionally carries only requirementRef + documentId.
    # Permit the runtime role to discover exactly that one Booking requirement before
    # tenant context is known. The application sets both values only after validating
    # an aud=audit Security ServiceIntegration token. All subsequent reads/writes use
    # the normal tenant RLS policy.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.current_internal_service_id()
        RETURNS varchar
        LANGUAGE sql
        STABLE
        AS $$
            SELECT NULLIF(current_setting('app.internal_service_id', true), '')::varchar;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.current_di_requirement_ref()
        RETURNS varchar
        LANGUAGE sql
        STABLE
        AS $$
            SELECT NULLIF(current_setting('app.di_requirement_ref', true), '')::varchar;
        $$
        """
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION auditcore.current_internal_service_id() TO {_RUNTIME_ROLE}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION auditcore.current_di_requirement_ref() TO {_RUNTIME_ROLE}"
    )
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


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS internal_di_booking_requirement_discovery "
        "ON auditcore.journey_document_requirements"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION auditcore.current_di_requirement_ref() FROM {_RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION auditcore.current_internal_service_id() FROM {_RUNTIME_ROLE}"
    )
    op.execute("DROP FUNCTION IF EXISTS auditcore.current_di_requirement_ref()")
    op.execute("DROP FUNCTION IF EXISTS auditcore.current_internal_service_id()")
