from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_authorized_work_items import _authorize_workspace
from audit_core.uc03_booking_capture import (
    _PROPOSAL_CAPTURE_MAP,
    _TERMINAL_PROCESSING_STATUSES,
    _document_views,
)

router = APIRouter(tags=["uc03-pc-verification"])
_FAILED_PROCESSING_STATUSES = {"FAILED", "ERROR", "REJECTED"}


class ReviewPendingItem(BaseModel):
    journeyId: UUID
    bookingReference: str | None
    customerDisplayName: str
    productLabel: str | None
    dealerName: str
    outletName: str
    bookingBusinessStatus: str | None
    captureCompletedAtUtc: datetime
    latestActivityAtUtc: datetime


class ReviewPendingPage(BaseModel):
    items: list[ReviewPendingItem]
    totalCount: int


def _review_readiness(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, int | bool]:
    documents = _document_views(connection, tenant_id, journey_id)
    linked = [item for item in documents if item["evidenceId"]]
    pending = 0
    failed = 0
    for item in linked:
        processing = (item["processingStatus"] or "").upper()
        if processing in _FAILED_PROCESSING_STATUSES:
            failed += 1
        elif processing not in _TERMINAL_PROCESSING_STATUSES:
            pending += 1

    pending_proposals = connection.execute(
        text(
            """
            SELECT count(*)
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
              AND proposal_status='PENDING'
              AND field_key = ANY(:reviewable_fields)
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "reviewable_fields": list(_PROPOSAL_CAPTURE_MAP),
        },
    ).scalar_one()
    return {
        "linkedDocumentCount": len(linked),
        "pendingDocumentCount": pending,
        "failedDocumentCount": failed,
        "pendingProposalCount": int(pending_proposals),
        "reviewReady": bool(linked) and pending == 0 and failed == 0,
    }








@router.get(
    "/v1/tenants/{tenant_id}/uc03/review-pending",
    response_model=ReviewPendingPage,
)
def list_review_pending(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ReviewPendingPage:
    _authorize_workspace(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    scope_sql = """
        FROM auditcore.journey_stage_states bs
        JOIN auditcore.journeys j
          ON j.tenant_id=bs.tenant_id AND j.journey_id=bs.journey_id
        JOIN auditcore.customers c
          ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
        JOIN auditcore.dealers d
          ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
        JOIN auditcore.dealer_outlets o
          ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
        LEFT JOIN auditcore.bookings b
          ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
        LEFT JOIN auditcore.journey_products jp
          ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
        WHERE bs.tenant_id=:tenant_id
          AND bs.stage_code='BOOKING'
          AND bs.capture_completed_at_utc IS NOT NULL
          AND bs.pc_verification_status='PENDING'
          AND EXISTS (
                SELECT 1
                FROM auditcore.business_assignments ba
                WHERE ba.tenant_id=j.tenant_id
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
          )
    """
    params = {"tenant_id": tenant_id, "actor_id": human_principal.subject, "limit": limit}
    total = connection.execute(text("SELECT count(*) " + scope_sql), params).scalar_one()
    rows = connection.execute(
        text(
            """
            SELECT bs.journey_id, b.booking_reference, c.display_name AS customer_display_name,
                   NULLIF(
                       concat_ws(
                           ' · ',
                           NULLIF(jp.model_name_snapshot, ''),
                           NULLIF(jp.variant_name_snapshot, ''),
                           NULLIF(jp.colour_name_snapshot, '')
                       ),
                       ''
                   ) AS product_label,
                   d.dealer_name, o.outlet_name,
                   COALESCE(bs.business_status, b.actual_status_code) AS booking_business_status,
                   bs.capture_completed_at_utc, bs.latest_activity_at_utc
            """ + scope_sql + """
            ORDER BY bs.latest_activity_at_utc DESC, bs.journey_id DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return ReviewPendingPage(
        totalCount=int(total),
        items=[
            ReviewPendingItem(
                journeyId=row["journey_id"],
                bookingReference=row["booking_reference"],
                customerDisplayName=row["customer_display_name"],
                productLabel=row["product_label"],
                dealerName=row["dealer_name"],
                outletName=row["outlet_name"],
                bookingBusinessStatus=row["booking_business_status"],
                captureCompletedAtUtc=row["capture_completed_at_utc"],
                latestActivityAtUtc=row["latest_activity_at_utc"],
            )
            for row in rows
        ],
    )
