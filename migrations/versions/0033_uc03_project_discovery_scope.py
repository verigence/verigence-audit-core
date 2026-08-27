"""Allow actor-scoped Dealer/Outlet reads during UC03 Project discovery.

Revision ID: 0033_uc03_project_scope
Revises: 0032_user_feedback
Create Date: 2026-08-27
"""
from alembic import op

revision = "0033_uc03_project_scope"
down_revision = "0032_user_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Project discovery already permits narrow cross-Tenant reads of Projects and
    # business_assignments for the authenticated Security actor. Extend that same
    # discovery path only to Dealer/Outlet rows explicitly assigned to the actor's
    # active PC role. This removes the per-Tenant N+1 lookup without granting broad
    # cross-Tenant Dealer/Outlet visibility or any write capability.
    op.execute(
        """
        CREATE POLICY actor_project_discovery_dealers
        ON auditcore.dealers
        FOR SELECT
        USING (
            auditcore.current_tenant_id() IS NULL
            AND auditcore.current_security_actor_id() IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM auditcore.business_assignments ba
                WHERE ba.tenant_id = dealers.tenant_id
                  AND ba.dealer_id = dealers.dealer_id
                  AND ba.security_actor_id = auditcore.current_security_actor_id()
                  AND ba.business_role_code = 'PC'
                  AND ba.assignment_status = 'ACTIVE'
                  AND ba.effective_from <= now()
                  AND (ba.effective_to IS NULL OR ba.effective_to >= now())
            )
        )
        """
    )
    op.execute(
        """
        CREATE POLICY actor_project_discovery_dealer_outlets
        ON auditcore.dealer_outlets
        FOR SELECT
        USING (
            auditcore.current_tenant_id() IS NULL
            AND auditcore.current_security_actor_id() IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM auditcore.business_assignments ba
                WHERE ba.tenant_id = dealer_outlets.tenant_id
                  AND ba.dealer_id = dealer_outlets.dealer_id
                  AND ba.outlet_id = dealer_outlets.outlet_id
                  AND ba.security_actor_id = auditcore.current_security_actor_id()
                  AND ba.business_role_code = 'PC'
                  AND ba.assignment_status = 'ACTIVE'
                  AND ba.effective_from <= now()
                  AND (ba.effective_to IS NULL OR ba.effective_to >= now())
            )
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS actor_project_discovery_dealer_outlets "
        "ON auditcore.dealer_outlets"
    )
    op.execute(
        "DROP POLICY IF EXISTS actor_project_discovery_dealers ON auditcore.dealers"
    )
