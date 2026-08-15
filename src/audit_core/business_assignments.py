from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError, require_tenant
from audit_core.security import Principal


def create_business_assignment(
    connection: Connection,
    *,
    tenant_id: str,
    security_actor_id: str,
    business_role_code: str,
    dealer_id: UUID | None = None,
    outlet_id: UUID | None = None,
    created_by_actor_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.business_assignments (
                tenant_id, security_actor_id, business_role_code,
                dealer_id, outlet_id, created_by_actor_id
            ) VALUES (
                :tenant_id, :security_actor_id, :business_role_code,
                :dealer_id, :outlet_id, :created_by_actor_id
            )
            RETURNING assignment_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "security_actor_id": security_actor_id,
            "business_role_code": business_role_code,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "created_by_actor_id": created_by_actor_id,
        },
    ).scalar_one()


def require_business_scope(
    connection: Connection,
    principal: Principal,
    *,
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID | None = None,
) -> None:
    require_tenant(principal, tenant_id)
    assigned = connection.execute(
        text(
            """
            SELECT 1
            FROM auditcore.business_assignments
            WHERE tenant_id = :tenant_id
              AND security_actor_id = :actor_id
              AND assignment_status = 'ACTIVE'
              AND effective_from <= now()
              AND (effective_to IS NULL OR effective_to >= now())
              AND (
                    dealer_id IS NULL
                    OR (
                        dealer_id = :dealer_id
                        AND (outlet_id IS NULL OR outlet_id = :outlet_id)
                    )
              )
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": principal.subject,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
        },
    ).scalar_one_or_none()
    if assigned is None:
        raise AuthorizationError(
            error_code="VAC-AUTH-004",
            status_code=403,
            title="Business scope denied",
        )
