from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import NotFoundError, ValidationError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-work-items"])
WorkType = Literal["ALL", "BOOKING", "DELIVERY"]


class StageSummary(BaseModel):
    businessStatus: str | None
    auditState: str
    auditStatus: str
    businessDate: date | None


class WorkItem(BaseModel):
    journeyId: UUID
    bookingReference: str | None
    customerDisplayName: str
    customerMobileLast4: str | None
    productLabel: str | None
    projectName: str
    dealerId: UUID
    dealerName: str
    outletId: UUID
    outletName: str
    booking: StageSummary
    delivery: StageSummary
    openFlagCount: int
    totalFlagCount: int
    highestOpenSeverity: str | None
    processingDocumentCount: int
    proposalReadyCount: int
    latestActivityAtUtc: datetime
    nextActionCode: str | None


class WorkItemFilters(BaseModel):
    workType: WorkType
    fromDate: date | None
    toDate: date | None
    timezoneName: str


class WorkItemPage(BaseModel):
    items: list[WorkItem]
    pageSize: int
    nextCursor: str | None
    previousCursor: str | None
    filters: WorkItemFilters


def _project_context(connection: Connection, tenant_id: str) -> tuple[str, str]:
    row = connection.execute(
        text(
            """
            SELECT project_name, timezone_name
            FROM auditcore.projects
            WHERE tenant_id = :tenant_id AND project_status = 'ACTIVE'
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-001",
            title="Project not found",
            detail="Active Project not found for the requested tenant.",
        )
    return str(row["project_name"]), str(row["timezone_name"])


def _filter_fingerprint(
    *,
    tenant_id: str,
    work_type: WorkType,
    from_date: date | None,
    to_date: date | None,
    timezone_name: str,
) -> str:
    canonical = json.dumps(
        {
            "v": 1,
            "tenantId": tenant_id,
            "workType": work_type,
            "fromDate": from_date.isoformat() if from_date else None,
            "toDate": to_date.isoformat() if to_date else None,
            "timezoneName": timezone_name,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _encode_cursor(*, latest_activity: datetime, journey_id: UUID, fingerprint: str) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "latestActivityAtUtc": latest_activity.isoformat(),
            "journeyId": str(journey_id),
            "filter": fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, *, fingerprint: str) -> tuple[datetime, UUID]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        if payload.get("v") != 1 or payload.get("filter") != fingerprint:
            raise ValueError("cursor/filter mismatch")
        latest_activity = datetime.fromisoformat(str(payload["latestActivityAtUtc"]))
        if latest_activity.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        journey_id = UUID(str(payload["journeyId"]))
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ValidationError(
            detail="Cursor is invalid or does not match the requested UC03 filters."
        ) from exc
    return latest_activity, journey_id


def _row_to_item(row, *, project_name: str) -> WorkItem:
    return WorkItem(
        journeyId=row["journey_id"],
        bookingReference=row["booking_reference"],
        customerDisplayName=row["customer_display_name"],
        customerMobileLast4=row["customer_mobile_last4"],
        productLabel=row["product_label"],
        projectName=project_name,
        dealerId=row["dealer_id"],
        dealerName=row["dealer_name"],
        outletId=row["outlet_id"],
        outletName=row["outlet_name"],
        booking=StageSummary(
            businessStatus=row["booking_business_status"],
            auditState=row["booking_audit_state"],
            auditStatus=row["booking_audit_status"],
            businessDate=row["booking_business_date"],
        ),
        delivery=StageSummary(
            businessStatus=row["delivery_business_status"],
            auditState=row["delivery_audit_state"],
            auditStatus=row["delivery_audit_status"],
            businessDate=row["delivery_business_date"],
        ),
        openFlagCount=int(row["open_flag_count"]),
        totalFlagCount=int(row["total_flag_count"]),
        highestOpenSeverity=row["highest_open_severity"],
        processingDocumentCount=int(row["processing_document_count"]),
        # Extraction proposal/acceptance persistence is a C1 concern. Keep the C0
        # read contract stable now without inventing a parallel proposal authority.
        proposalReadyCount=0,
        latestActivityAtUtc=row["latest_activity_at_utc"],
        # Next-action policy is introduced with concrete Booking/Delivery commands.
        # Returning null in C0 is deliberate rather than guessing workflow semantics.
        nextActionCode=None,
    )


@router.get("/work-items", response_model=WorkItemPage)
def list_work_items(
    tenant_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
    work_type: Annotated[WorkType, Query(alias="workType")] = "ALL",
    from_date: Annotated[date | None, Query(alias="fromDate")] = None,
    to_date: Annotated[date | None, Query(alias="toDate")] = None,
    limit: Annotated[int, Query(ge=1, le=10)] = 10,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> WorkItemPage:
    """Return the latest authorized UC03 Booking/Delivery work items.

    Date precedence frozen for C0 contract tests:
    - Booking: source Booking date, else Booking first-start event date, else Booking record date.
    - Delivery: actual-delivered date when present, else Delivery first-start event date,
      else Delivery record date.
    All timestamp-to-date conversion uses the selected Project timezone.
    """

    authorize(principal, tenant_id=tenant_id, permission="audit.journey.read")
    set_tenant_context(connection, tenant_id)
    project_name, timezone_name = _project_context(connection, tenant_id)

    if from_date is not None and to_date is not None and from_date > to_date:
        raise ValidationError(detail="fromDate must be on or before toDate.")

    fingerprint = _filter_fingerprint(
        tenant_id=tenant_id,
        work_type=work_type,
        from_date=from_date,
        to_date=to_date,
        timezone_name=timezone_name,
    )
    cursor_at: datetime | None = None
    cursor_journey_id: UUID | None = None
    if cursor:
        cursor_at, cursor_journey_id = _decode_cursor(cursor, fingerprint=fingerprint)

    rows = list(
        connection.execute(
            text(
                """
                WITH finding_counts AS (
                    SELECT
                        journey_id,
                        count(*) FILTER (
                            WHERE finding_status IN ('OPEN','ACKNOWLEDGED')
                        ) AS open_flag_count,
                        count(*) FILTER (
                            WHERE finding_status <> 'VOIDED'
                        ) AS total_flag_count,
                        (
                            array_agg(
                                severity
                                ORDER BY
                                    CASE severity
                                        WHEN 'CRITICAL' THEN 5
                                        WHEN 'HIGH' THEN 4
                                        WHEN 'MEDIUM' THEN 3
                                        WHEN 'LOW' THEN 2
                                        WHEN 'INFO' THEN 1
                                        ELSE 0
                                    END DESC,
                                    severity
                            ) FILTER (
                                WHERE finding_status IN ('OPEN','ACKNOWLEDGED')
                            )
                        )[1] AS highest_open_severity,
                        max(updated_at_utc) AS latest_finding_activity
                    FROM auditcore.audit_findings
                    WHERE tenant_id = :tenant_id
                    GROUP BY journey_id
                ),
                evidence_activity AS (
                    SELECT
                        journey_id,
                        max(COALESCE(cache_updated_at_utc, linked_at_utc)) AS latest_evidence_activity
                    FROM auditcore.evidence
                    WHERE tenant_id = :tenant_id
                      AND association_status = 'ACTIVE'
                    GROUP BY journey_id
                ),
                processing_counts AS (
                    SELECT
                        journey_id,
                        count(*) FILTER (
                            WHERE operation_status IN (
                                'RECEIVED','DI_SUBMITTING','DI_ACCEPTED','RETRY_WAIT'
                            )
                        ) AS processing_document_count,
                        max(updated_at_utc) FILTER (
                            WHERE operation_status IN (
                                'RECEIVED','DI_SUBMITTING','DI_ACCEPTED','RETRY_WAIT'
                            )
                        ) AS latest_processing_activity
                    FROM auditcore.evidence_ingestion_operations
                    WHERE tenant_id = :tenant_id
                    GROUP BY journey_id
                ),
                base AS (
                    SELECT
                        j.journey_id,
                        j.dealer_id,
                        j.outlet_id,
                        c.display_name AS customer_display_name,
                        c.mobile_last4 AS customer_mobile_last4,
                        d.dealer_name,
                        o.outlet_name,
                        (b.booking_id IS NOT NULL OR bs.journey_id IS NOT NULL) AS has_booking,
                        (dl.delivery_id IS NOT NULL OR ds.journey_id IS NOT NULL) AS has_delivery,
                        b.booking_reference,
                        NULLIF(
                            concat_ws(
                                ' · ',
                                NULLIF(jp.model_name_snapshot, ''),
                                NULLIF(jp.variant_name_snapshot, ''),
                                NULLIF(jp.colour_name_snapshot, '')
                            ),
                            ''
                        ) AS product_label,
                        COALESCE(bs.business_status, b.actual_status_code) AS booking_business_status,
                        COALESCE(bs.audit_state, 'NOT_STARTED') AS booking_audit_state,
                        COALESCE(bs.audit_status, 'NOT_EVALUATED') AS booking_audit_status,
                        COALESCE(
                            b.booking_date,
                            (bs.first_started_at_utc AT TIME ZONE :timezone_name)::date,
                            (b.created_at_utc AT TIME ZONE :timezone_name)::date
                        ) AS booking_business_date,
                        COALESCE(ds.business_status, dl.actual_delivery_status_code) AS delivery_business_status,
                        COALESCE(ds.audit_state, 'NOT_STARTED') AS delivery_audit_state,
                        COALESCE(ds.audit_status, 'NOT_EVALUATED') AS delivery_audit_status,
                        COALESCE(
                            (dl.actual_delivered_at AT TIME ZONE :timezone_name)::date,
                            (ds.first_started_at_utc AT TIME ZONE :timezone_name)::date,
                            (dl.created_at_utc AT TIME ZONE :timezone_name)::date
                        ) AS delivery_business_date,
                        COALESCE(fc.open_flag_count, 0) AS open_flag_count,
                        COALESCE(fc.total_flag_count, 0) AS total_flag_count,
                        fc.highest_open_severity,
                        COALESCE(pc.processing_document_count, 0) AS processing_document_count,
                        GREATEST(
                            j.updated_at_utc,
                            bs.latest_activity_at_utc,
                            ds.latest_activity_at_utc,
                            b.updated_at_utc,
                            dl.updated_at_utc,
                            ea.latest_evidence_activity,
                            fc.latest_finding_activity,
                            pc.latest_processing_activity
                        ) AS latest_activity_at_utc
                    FROM auditcore.journeys j
                    JOIN auditcore.customers c
                      ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
                    JOIN auditcore.dealers d
                      ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
                    JOIN auditcore.dealer_outlets o
                      ON o.tenant_id = j.tenant_id
                     AND o.dealer_id = j.dealer_id
                     AND o.outlet_id = j.outlet_id
                    LEFT JOIN auditcore.bookings b
                      ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
                    LEFT JOIN auditcore.deliveries dl
                      ON dl.tenant_id = j.tenant_id AND dl.journey_id = j.journey_id
                    LEFT JOIN auditcore.journey_products jp
                      ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
                    LEFT JOIN auditcore.journey_stage_states bs
                      ON bs.tenant_id = j.tenant_id
                     AND bs.journey_id = j.journey_id
                     AND bs.stage_code = 'BOOKING'
                    LEFT JOIN auditcore.journey_stage_states ds
                      ON ds.tenant_id = j.tenant_id
                     AND ds.journey_id = j.journey_id
                     AND ds.stage_code = 'DELIVERY'
                    LEFT JOIN finding_counts fc ON fc.journey_id = j.journey_id
                    LEFT JOIN evidence_activity ea ON ea.journey_id = j.journey_id
                    LEFT JOIN processing_counts pc ON pc.journey_id = j.journey_id
                    WHERE j.tenant_id = :tenant_id
                      AND (b.booking_id IS NOT NULL OR dl.delivery_id IS NOT NULL
                           OR bs.journey_id IS NOT NULL OR ds.journey_id IS NOT NULL)
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
                filtered AS (
                    SELECT *
                    FROM base
                    WHERE
                        (
                            :work_type = 'ALL'
                            OR (:work_type = 'BOOKING' AND has_booking)
                            OR (:work_type = 'DELIVERY' AND has_delivery)
                        )
                        AND (
                            (:from_date IS NULL AND :to_date IS NULL)
                            OR (
                                :work_type IN ('ALL','BOOKING')
                                AND has_booking
                                AND booking_business_date IS NOT NULL
                                AND (:from_date IS NULL OR booking_business_date >= :from_date)
                                AND (:to_date IS NULL OR booking_business_date <= :to_date)
                            )
                            OR (
                                :work_type IN ('ALL','DELIVERY')
                                AND has_delivery
                                AND delivery_business_date IS NOT NULL
                                AND (:from_date IS NULL OR delivery_business_date >= :from_date)
                                AND (:to_date IS NULL OR delivery_business_date <= :to_date)
                            )
                        )
                        AND (
                            :cursor_at IS NULL
                            OR latest_activity_at_utc < :cursor_at
                            OR (
                                latest_activity_at_utc = :cursor_at
                                AND journey_id < :cursor_journey_id
                            )
                        )
                )
                SELECT *
                FROM filtered
                ORDER BY latest_activity_at_utc DESC, journey_id DESC
                LIMIT :fetch_limit
                """
            ),
            {
                "tenant_id": tenant_id,
                "actor_id": principal.subject,
                "timezone_name": timezone_name,
                "work_type": work_type,
                "from_date": from_date,
                "to_date": to_date,
                "cursor_at": cursor_at,
                "cursor_journey_id": cursor_journey_id,
                "fetch_limit": limit + 1,
            },
        ).mappings()
    )

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [_row_to_item(row, project_name=project_name) for row in page_rows]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(
            latest_activity=last["latest_activity_at_utc"],
            journey_id=last["journey_id"],
            fingerprint=fingerprint,
        )

    return WorkItemPage(
        items=items,
        pageSize=len(items),
        nextCursor=next_cursor,
        previousCursor=None,
        filters=WorkItemFilters(
            workType=work_type,
            fromDate=from_date,
            toDate=to_date,
            timezoneName=timezone_name,
        ),
    )
