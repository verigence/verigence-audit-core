from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_authorized_work_items import _authorize_workspace

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03/tl", tags=["uc03-tl-supervisory"])


class TlSupervisoryCase(BaseModel):
    journeyId: UUID
    bookingReference: str | None
    customerDisplayName: str
    customerMobileLast4: str | None
    productLabel: str | None
    dealerId: UUID
    dealerName: str
    outletId: UUID
    outletName: str
    bookingBusinessStatus: str | None
    bookingBusinessDate: date | None
    bookingSubmittedAtUtc: datetime | None
    pcVerificationStatus: str | None
    deliveryBusinessStatus: str | None
    deliveryBusinessDate: date | None
    responsiblePcActorId: str | None
    openFlagCount: int
    highestOpenSeverity: str | None
    latestActivityAtUtc: datetime


class TlSupervisoryCasePage(BaseModel):
    items: list[TlSupervisoryCase]
    totalCount: int
    limit: int
    offset: int


def _require_tl_scope(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
) -> None:
    """Require the current actor to have an active TL business assignment.

    TL scope is Dealer-wide by design. The case query below reuses the same active
    assignment predicate so the endpoint cannot widen a TL beyond assigned Dealers.
    """

    assigned = connection.execute(
        text(
            """
            SELECT 1
            FROM auditcore.business_assignments
            WHERE tenant_id=:tenant_id
              AND security_actor_id=:actor_id
              AND business_role_code='TL'
              AND assignment_status='ACTIVE'
              AND effective_from <= now()
              AND (effective_to IS NULL OR effective_to >= now())
              AND dealer_id IS NOT NULL
              AND outlet_id IS NULL
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "actor_id": actor_id},
    ).scalar_one_or_none()
    if assigned is None:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


_SCOPE_SQL = """
    FROM auditcore.journeys j
    JOIN auditcore.customers c
      ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
    JOIN auditcore.dealers dealer
      ON dealer.tenant_id=j.tenant_id AND dealer.dealer_id=j.dealer_id
    JOIN auditcore.dealer_outlets outlet
      ON outlet.tenant_id=j.tenant_id
     AND outlet.dealer_id=j.dealer_id
     AND outlet.outlet_id=j.outlet_id
    LEFT JOIN auditcore.bookings b
      ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
    LEFT JOIN auditcore.deliveries delivery
      ON delivery.tenant_id=j.tenant_id AND delivery.journey_id=j.journey_id
    LEFT JOIN auditcore.journey_products jp
      ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
    LEFT JOIN auditcore.journey_stage_states bs
      ON bs.tenant_id=j.tenant_id
     AND bs.journey_id=j.journey_id
     AND bs.stage_code='BOOKING'
    LEFT JOIN auditcore.journey_stage_states ds
      ON ds.tenant_id=j.tenant_id
     AND ds.journey_id=j.journey_id
     AND ds.stage_code='DELIVERY'
    WHERE j.tenant_id=:tenant_id
      AND EXISTS (
            SELECT 1
            FROM auditcore.business_assignments tl
            WHERE tl.tenant_id=j.tenant_id
              AND tl.security_actor_id=:actor_id
              AND tl.business_role_code='TL'
              AND tl.assignment_status='ACTIVE'
              AND tl.effective_from <= now()
              AND (tl.effective_to IS NULL OR tl.effective_to >= now())
              AND tl.dealer_id=j.dealer_id
              AND tl.outlet_id IS NULL
      )
      AND (
            bs.capture_completed_at_utc IS NOT NULL
            OR delivery.delivery_id IS NOT NULL
            OR ds.business_status IS NOT NULL
      )
"""


@router.get("/cases", response_model=TlSupervisoryCasePage)
def list_tl_supervisory_cases(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TlSupervisoryCasePage:
    """Return PC-submitted/progressed cases for the TL's assigned Dealer scope.

    The endpoint intentionally returns case facts rather than a pre-shaped dashboard.
    Web owns the supervisory presentation, grouping and filters. PC drafts are kept
    private until Booking capture has been submitted (or Delivery has progressed).
    """

    _authorize_workspace(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    _require_tl_scope(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
    )

    params = {
        "tenant_id": tenant_id,
        "actor_id": human_principal.subject,
        "limit": limit,
        "offset": offset,
    }
    total = connection.execute(
        text("SELECT count(*) " + _SCOPE_SQL),
        params,
    ).scalar_one()

    rows = connection.execute(
        text(
            """
            SELECT
                j.journey_id,
                b.booking_reference,
                c.display_name AS customer_display_name,
                c.mobile_last4 AS customer_mobile_last4,
                NULLIF(
                    concat_ws(
                        ' · ',
                        NULLIF(jp.model_name_snapshot, ''),
                        NULLIF(jp.variant_name_snapshot, ''),
                        NULLIF(jp.colour_name_snapshot, '')
                    ),
                    ''
                ) AS product_label,
                j.dealer_id,
                dealer.dealer_name,
                j.outlet_id,
                outlet.outlet_name,
                COALESCE(bs.business_status, b.actual_status_code) AS booking_business_status,
                COALESCE(
                    b.booking_date,
                    (bs.first_started_at_utc AT TIME ZONE 'UTC')::date,
                    (b.created_at_utc AT TIME ZONE 'UTC')::date
                ) AS booking_business_date,
                bs.capture_completed_at_utc AS booking_submitted_at_utc,
                bs.pc_verification_status,
                COALESCE(ds.business_status, delivery.actual_delivery_status_code)
                    AS delivery_business_status,
                COALESCE(
                    (delivery.actual_delivered_at AT TIME ZONE 'UTC')::date,
                    (ds.first_started_at_utc AT TIME ZONE 'UTC')::date,
                    (delivery.created_at_utc AT TIME ZONE 'UTC')::date
                ) AS delivery_business_date,
                pc_submit.actor_id AS responsible_pc_actor_id,
                COALESCE(findings.open_flag_count, 0) AS open_flag_count,
                findings.highest_open_severity,
                GREATEST(
                    j.updated_at_utc,
                    bs.latest_activity_at_utc,
                    ds.latest_activity_at_utc,
                    b.updated_at_utc,
                    delivery.updated_at_utc,
                    findings.latest_finding_activity,
                    pc_submit.recorded_at_utc
                ) AS latest_activity_at_utc
            """
            + _SCOPE_SQL
            + """
            LEFT JOIN LATERAL (
                SELECT e.actor_id, e.recorded_at_utc
                FROM auditcore.journey_workflow_events e
                WHERE e.tenant_id=j.tenant_id
                  AND e.journey_id=j.journey_id
                  AND e.stage_code='BOOKING'
                  AND e.event_type='PC_BOOKING_CAPTURE_SUBMITTED'
                  AND e.source_kind='HUMAN'
                  AND e.actor_id IS NOT NULL
                ORDER BY e.recorded_at_utc DESC, e.event_id DESC
                LIMIT 1
            ) pc_submit ON true
            LEFT JOIN LATERAL (
                SELECT
                    count(*) FILTER (
                        WHERE f.finding_status IN ('OPEN','ACKNOWLEDGED')
                    ) AS open_flag_count,
                    (
                        array_agg(
                            f.severity
                            ORDER BY
                                CASE f.severity
                                    WHEN 'CRITICAL' THEN 5
                                    WHEN 'HIGH' THEN 4
                                    WHEN 'MEDIUM' THEN 3
                                    WHEN 'LOW' THEN 2
                                    WHEN 'INFO' THEN 1
                                    ELSE 0
                                END DESC,
                                f.severity
                        ) FILTER (
                            WHERE f.finding_status IN ('OPEN','ACKNOWLEDGED')
                        )
                    )[1] AS highest_open_severity,
                    max(f.updated_at_utc) AS latest_finding_activity
                FROM auditcore.audit_findings f
                WHERE f.tenant_id=j.tenant_id AND f.journey_id=j.journey_id
            ) findings ON true
            ORDER BY latest_activity_at_utc DESC, j.journey_id DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    return TlSupervisoryCasePage(
        totalCount=int(total),
        limit=limit,
        offset=offset,
        items=[
            TlSupervisoryCase(
                journeyId=row["journey_id"],
                bookingReference=row["booking_reference"],
                customerDisplayName=row["customer_display_name"],
                customerMobileLast4=row["customer_mobile_last4"],
                productLabel=row["product_label"],
                dealerId=row["dealer_id"],
                dealerName=row["dealer_name"],
                outletId=row["outlet_id"],
                outletName=row["outlet_name"],
                bookingBusinessStatus=row["booking_business_status"],
                bookingBusinessDate=row["booking_business_date"],
                bookingSubmittedAtUtc=row["booking_submitted_at_utc"],
                pcVerificationStatus=row["pc_verification_status"],
                deliveryBusinessStatus=row["delivery_business_status"],
                deliveryBusinessDate=row["delivery_business_date"],
                responsiblePcActorId=row["responsible_pc_actor_id"],
                openFlagCount=int(row["open_flag_count"]),
                highestOpenSeverity=row["highest_open_severity"],
                latestActivityAtUtc=row["latest_activity_at_utc"],
            )
            for row in rows
        ],
    )
