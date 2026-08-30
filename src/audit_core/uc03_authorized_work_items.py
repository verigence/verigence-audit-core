from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import DependencyUnavailableError, NotFoundError
from audit_core.security import HumanPrincipal, Principal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.uc03_work_items import WorkItemPage, WorkType, list_work_items

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-work-items"])
_PERMISSION_KEY = "audit.journey.read"


class LandingMetrics(BaseModel):
    bookingsInProgress: int
    deliveryInProgress: int
    reviewPending: int
    needsAttention: int
    auditFlags: int
    auditInProgress: int


class DashboardBootstrap(BaseModel):
    metrics: LandingMetrics
    workItems: WorkItemPage


def _authorize_workspace(
    client: SecurityAuthorizationClient,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=_PERMISSION_KEY,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="Project work is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


def _delegated_principal(human_principal: HumanPrincipal, tenant_id: str) -> Principal:
    return Principal(
        subject=human_principal.subject,
        tenant_id=tenant_id,
        permissions=(_PERMISSION_KEY,),
    )


@router.get("/landing-metrics", response_model=LandingMetrics)
def get_landing_metrics(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    outlet_id: Annotated[UUID | None, Query(alias="outletId")] = None,
) -> LandingMetrics:
    """Return landing counters for the actor's current working scope.

    For a PC dashboard, Web supplies the Outlet selected before entering the dashboard;
    the same existing business-assignment predicate remains the authorization boundary.
    """

    _authorize_workspace(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    active_project = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.projects
            WHERE tenant_id = :tenant_id AND project_status = 'ACTIVE'
            """
        ),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    if active_project is None:
        raise NotFoundError(
            error_code="VAC-NF-001",
            title="Project not found",
            detail="Active Project not found for the requested tenant.",
        )

    row = connection.execute(
        text(
            """
            WITH scoped AS (
                SELECT j.journey_id
                FROM auditcore.journeys j
                WHERE j.tenant_id = :tenant_id
                  AND (
                        CAST(:outlet_id AS uuid) IS NULL
                        OR j.outlet_id = CAST(:outlet_id AS uuid)
                  )
                  AND EXISTS (
                        SELECT 1
                        FROM auditcore.business_assignments ba
                        WHERE ba.tenant_id = j.tenant_id
                          AND ba.security_actor_id = :actor_id
                          AND ba.assignment_status = 'ACTIVE'
                          AND ba.effective_from <= now()
                          AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                          AND (
                                ba.dealer_id IS NULL
                                OR (
                                    ba.dealer_id = j.dealer_id
                                    AND (ba.outlet_id IS NULL OR ba.outlet_id = j.outlet_id)
                                )
                          )
                  )
            ),
            stage_projection AS (
                SELECT
                    s.journey_id,
                    COALESCE(bs.business_status, b.actual_status_code) AS booking_status,
                    COALESCE(ds.business_status, d.actual_delivery_status_code) AS delivery_status,
                    COALESCE(bs.audit_state, 'NOT_STARTED') AS booking_audit_state,
                    COALESCE(ds.audit_state, 'NOT_STARTED') AS delivery_audit_state,
                    bs.capture_completed_at_utc AS booking_capture_completed_at_utc,
                    bs.pc_verification_status AS booking_pc_verification_status
                FROM scoped s
                LEFT JOIN auditcore.bookings b
                  ON b.tenant_id = :tenant_id AND b.journey_id = s.journey_id
                LEFT JOIN auditcore.deliveries d
                  ON d.tenant_id = :tenant_id AND d.journey_id = s.journey_id
                LEFT JOIN auditcore.journey_stage_states bs
                  ON bs.tenant_id = :tenant_id
                 AND bs.journey_id = s.journey_id
                 AND bs.stage_code = 'BOOKING'
                LEFT JOIN auditcore.journey_stage_states ds
                  ON ds.tenant_id = :tenant_id
                 AND ds.journey_id = s.journey_id
                 AND ds.stage_code = 'DELIVERY'
            ),
            active_findings AS (
                SELECT f.journey_id, count(*) AS flag_count
                FROM auditcore.audit_findings f
                JOIN scoped s ON s.journey_id = f.journey_id
                WHERE f.tenant_id = :tenant_id
                  AND f.finding_status IN ('OPEN','ACKNOWLEDGED')
                GROUP BY f.journey_id
            )
            SELECT
                count(*) FILTER (
                    WHERE booking_status IN ('BOOKING_STARTED','BOOKING_IN_PROGRESS')
                ) AS bookings_in_progress,
                count(*) FILTER (
                    WHERE delivery_status IN ('DELIVERY_STARTED','DELIVERY_IN_PROGRESS')
                ) AS delivery_in_progress,
                count(*) FILTER (
                    WHERE booking_capture_completed_at_utc IS NOT NULL
                      AND booking_pc_verification_status = 'PENDING'
                ) AS review_pending,
                count(*) FILTER (
                    WHERE booking_audit_state = 'IN_PROGRESS'
                       OR delivery_audit_state = 'IN_PROGRESS'
                ) AS audit_in_progress,
                (SELECT count(*) FROM active_findings) AS needs_attention,
                COALESCE((SELECT sum(flag_count) FROM active_findings), 0) AS audit_flags
            FROM stage_projection
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": human_principal.subject,
            "outlet_id": outlet_id,
        },
    ).mappings().one()
    return LandingMetrics(
        bookingsInProgress=int(row["bookings_in_progress"]),
        deliveryInProgress=int(row["delivery_in_progress"]),
        reviewPending=int(row["review_pending"]),
        needsAttention=int(row["needs_attention"]),
        auditFlags=int(row["audit_flags"]),
        auditInProgress=int(row["audit_in_progress"]),
    )


@router.get("/work-items", response_model=WorkItemPage)
def list_authorized_work_items(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    work_type: Annotated[WorkType, Query(alias="workType")] = "ALL",
    from_date: Annotated[date | None, Query(alias="fromDate")] = None,
    to_date: Annotated[date | None, Query(alias="toDate")] = None,
    outlet_id: Annotated[UUID | None, Query(alias="outletId")] = None,
    limit: Annotated[int, Query(ge=1, le=10)] = 10,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> WorkItemPage:
    """Return work items after live Security authorization and business scoping."""

    _authorize_workspace(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    return list_work_items(
        tenant_id=tenant_id,
        principal=_delegated_principal(human_principal, tenant_id),
        connection=connection,
        work_type=work_type,
        from_date=from_date,
        to_date=to_date,
        outlet_id=outlet_id,
        limit=limit,
        cursor=cursor,
    )


@router.get("/dashboard", response_model=DashboardBootstrap)
def get_dashboard_bootstrap(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    outlet_id: Annotated[UUID | None, Query(alias="outletId")] = None,
) -> DashboardBootstrap:
    """Return landing metrics and the first work-items page in one page-bootstrap call."""

    metrics = get_landing_metrics(
        tenant_id=tenant_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
        outlet_id=outlet_id,
    )
    work_items = list_authorized_work_items(
        tenant_id=tenant_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
        work_type="ALL",
        from_date=None,
        to_date=None,
        outlet_id=outlet_id,
        limit=10,
        cursor=None,
    )
    return DashboardBootstrap(metrics=metrics, workItems=work_items)
