"""Single Phase 2 access adapter.

Security owns authentication and functional permission. Audit Core business
assignments own Journey data scope (dealer/outlet and operating role). P2 routes
must call this adapter rather than independently composing those checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.errors import DependencyUnavailableError, NotFoundError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
)


@dataclass(frozen=True)
class P2AccessContext:
    actor_id: str
    tenant_id: str
    functional_role: str | None
    operating_role: str | None


def check_p2_permission(
    *,
    tenant_id: str,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
    permission_key: str,
):
    """Security's functional-permission decision. Call before any DB work:
    it may perform a network call (allow decisions are process-cached)."""
    try:
        decision = authorization_client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=permission_key,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="Phase 2 work is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )
    return decision


def resolve_p2_scope(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID | None,
    human_principal: HumanPrincipal,
    decision,
) -> P2AccessContext:
    """Set Tenant context and resolve the caller's data scope for a Journey.

    Security decides *what* the caller may do; Audit Core business
    assignments decide *which* dealer/outlet Journeys the caller operates on.
    The scope lookup never grants a permission Security denied."""
    set_tenant_context(connection, tenant_id)
    operating_role: str | None = None
    if journey_id is not None:
        row = connection.execute(
            text(
                """
                SELECT array_agg(DISTINCT ba.business_role_code ORDER BY ba.business_role_code)
                FROM auditcore.journeys j
                JOIN auditcore.business_assignments ba
                  ON ba.tenant_id=j.tenant_id
                 AND ba.security_actor_id=:actor_id
                 AND ba.assignment_status='ACTIVE'
                 AND ba.effective_from <= now()
                 AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                 AND (
                      ba.dealer_id IS NULL
                      OR (
                           ba.dealer_id=j.dealer_id
                           AND (ba.outlet_id IS NULL OR ba.outlet_id=j.outlet_id)
                      )
                 )
                WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "actor_id": human_principal.subject,
            },
        ).scalar_one_or_none()
        roles = list(row or [])
        if not roles:
            # Do not disclose whether a Journey exists outside caller scope.
            raise NotFoundError(
                error_code="VAC-NF-005",
                title="Journey not found",
                detail="Journey not found in your current Project scope.",
            )
        if len(roles) > 1:
            raise AuthorizationError(
                error_code="VAC-AUTH-002",
                status_code=403,
                title="Ambiguous operating role",
            )
        operating_role = str(roles[0])

    return P2AccessContext(
        actor_id=human_principal.subject,
        tenant_id=tenant_id,
        functional_role=decision.role_key,
        operating_role=operating_role,
    )


def authorize_p2(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID | None,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
    permission_key: str,
) -> P2AccessContext:
    decision = check_p2_permission(
        tenant_id=tenant_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=permission_key,
    )
    return resolve_p2_scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        decision=decision,
    )
