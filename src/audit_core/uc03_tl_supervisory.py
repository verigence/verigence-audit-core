from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, DependencyUnavailableError, NotFoundError
from audit_core.evidence import _external_context_ref
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.uc03_authorized_work_items import _authorize_workspace
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _stage_state,
)
from audit_core.uc03_pc_generic_review import (
    DirectExtractedField,
    _project_known_field,
    _store_fields,
    _validate_unique_fields,
)
from audit_core.workflow import create_workflow_task

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03/tl", tags=["uc03-tl-supervisory"])

_REVIEW_READ_PERMISSION = "audit.review.read"
_REVIEW_DECIDE_PERMISSION = "audit.review.decide"
_ACTIVE_TASK_STATUSES = ("PENDING", "READY", "CLAIMED", "IN_PROGRESS", "RETRY_WAIT")


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


class TlReviewRequirement(BaseModel):
    requirementRef: UUID
    requirementKey: str
    documentTypeKey: str
    activeDocumentIds: list[UUID] = Field(default_factory=list)


class TlReviewContext(BaseModel):
    journeyId: UUID
    externalContextRef: str
    requirements: list[TlReviewRequirement]


class TlDocumentReviewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirementRef: UUID
    documentId: UUID
    fields: list[DirectExtractedField] = Field(default_factory=list, max_length=500)


class TlDocumentReviewResponse(BaseModel):
    journeyId: UUID
    requirementRef: UUID
    documentId: UUID
    aggregateVersion: int
    reviewEventId: UUID
    storedFieldCount: int
    modifiedFieldCount: int
    projectedFieldCount: int
    projectionFailureCount: int


class TlReuploadRequestCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirementRef: UUID
    documentId: UUID
    reason: str = Field(min_length=1, max_length=1000)


class TlReuploadRequestResponse(BaseModel):
    journeyId: UUID
    requirementRef: UUID
    documentId: UUID
    taskId: UUID
    findingId: UUID
    assignedPcActorId: str
    status: str = "REQUESTED"


def _authorize_permission(
    client: SecurityAuthorizationClient,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
    permission_key: str,
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=permission_key,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="Team Lead review is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


def _require_tl_case_scope(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    journey_id: UUID | None = None,
) -> dict[str, Any] | None:
    """Require an active Dealer-wide TL assignment.

    TL scope is Dealer-wide, never Outlet-only. When a Journey is supplied this also
    proves that the Journey belongs to one of those Dealers and has crossed the PC
    submission/progression boundary. TL review therefore cannot expose PC drafts.
    """

    if journey_id is None:
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
        return None

    row = connection.execute(
        text(
            """
            SELECT j.journey_id, j.customer_id, j.dealer_id, j.outlet_id,
                   bs.capture_completed_at_utc,
                   COALESCE(bs.business_status, b.actual_status_code) AS booking_status,
                   COALESCE(ds.business_status, d.actual_delivery_status_code) AS delivery_status
            FROM auditcore.journeys j
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
            LEFT JOIN auditcore.deliveries d
              ON d.tenant_id=j.tenant_id AND d.journey_id=j.journey_id
            LEFT JOIN auditcore.journey_stage_states bs
              ON bs.tenant_id=j.tenant_id
             AND bs.journey_id=j.journey_id
             AND bs.stage_code='BOOKING'
            LEFT JOIN auditcore.journey_stage_states ds
              ON ds.tenant_id=j.tenant_id
             AND ds.journey_id=j.journey_id
             AND ds.stage_code='DELIVERY'
            WHERE j.tenant_id=:tenant_id
              AND j.journey_id=:journey_id
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
                    OR d.delivery_id IS NOT NULL
                    OR ds.business_status IS NOT NULL
              )
            """
        ),
        {"tenant_id": tenant_id, "actor_id": actor_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Submitted case not found",
            detail="The submitted Booking/Delivery case is not available in this Team Lead scope.",
        )
    return dict(row)


_BASE_FROM_SQL = """
    FROM auditcore.journeys j
    JOIN auditcore.customers c
      ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
    JOIN auditcore.projects project
      ON project.tenant_id=j.tenant_id AND project.project_status='ACTIVE'
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
"""

_SCOPE_WHERE_SQL = """
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
    """Return submitted/progressed cases for the TL's assigned Dealer scope."""

    _authorize_workspace(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    _require_tl_case_scope(
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
        text("SELECT count(*) " + _BASE_FROM_SQL + _SCOPE_WHERE_SQL),
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
                    (bs.first_started_at_utc AT TIME ZONE project.timezone_name)::date,
                    (b.created_at_utc AT TIME ZONE project.timezone_name)::date
                ) AS booking_business_date,
                bs.capture_completed_at_utc AS booking_submitted_at_utc,
                bs.pc_verification_status,
                COALESCE(ds.business_status, delivery.actual_delivery_status_code)
                    AS delivery_business_status,
                COALESCE(
                    (delivery.actual_delivered_at AT TIME ZONE project.timezone_name)::date,
                    (ds.first_started_at_utc AT TIME ZONE project.timezone_name)::date,
                    (delivery.created_at_utc AT TIME ZONE project.timezone_name)::date
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
                    pc_submit.occurred_at_utc
                ) AS latest_activity_at_utc
            """
            + _BASE_FROM_SQL
            + """
            LEFT JOIN LATERAL (
                SELECT e.actor_id, e.occurred_at_utc
                FROM auditcore.journey_workflow_events e
                WHERE e.tenant_id=j.tenant_id
                  AND e.journey_id=j.journey_id
                  AND e.stage_code='BOOKING'
                  AND e.event_type='PC_BOOKING_CAPTURE_SUBMITTED'
                  AND e.source_kind='HUMAN'
                  AND e.actor_id IS NOT NULL
                ORDER BY e.occurred_at_utc DESC, e.event_id DESC
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
            """
            + _SCOPE_WHERE_SQL
            + """
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


@router.get("/cases/{journey_id}/review-context", response_model=TlReviewContext)
def get_tl_review_context(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TlReviewContext:
    _authorize_permission(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
        permission_key=_REVIEW_READ_PERMISSION,
    )
    set_tenant_context(connection, tenant_id)
    case = _require_tl_case_scope(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        journey_id=journey_id,
    )
    assert case is not None

    rows = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id,
                   jdr.requirement_key,
                   jdr.document_type_key,
                   ARRAY(
                       SELECT e.di_document_id
                       FROM auditcore.evidence e
                       WHERE e.tenant_id=jdr.tenant_id
                         AND e.journey_id=jdr.journey_id
                         AND e.journey_document_requirement_id=jdr.journey_document_requirement_id
                         AND e.association_status='ACTIVE'
                         AND e.di_document_id IS NOT NULL
                       ORDER BY e.linked_at_utc, e.evidence_id
                   ) AS active_document_ids
            FROM auditcore.journey_document_requirements jdr
            WHERE jdr.tenant_id=:tenant_id
              AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='BOOKING'
            ORDER BY jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    requirements = [
        TlReviewRequirement(
            requirementRef=row["journey_document_requirement_id"],
            requirementKey=row["requirement_key"],
            documentTypeKey=row["document_type_key"],
            activeDocumentIds=list(row["active_document_ids"] or []),
        )
        for row in rows
        if row["active_document_ids"]
    ]
    return TlReviewContext(
        journeyId=journey_id,
        externalContextRef=_external_context_ref(
            journey_id=journey_id,
            customer_id=case["customer_id"],
        ),
        requirements=requirements,
    )


def _linked_document(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirement_ref: UUID,
    document_id: UUID,
):
    row = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.di_document_id, jdr.document_type_key
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_requirements jdr
              ON jdr.tenant_id=e.tenant_id
             AND jdr.journey_id=e.journey_id
             AND jdr.journey_document_requirement_id=e.journey_document_requirement_id
            WHERE e.tenant_id=:tenant_id
              AND e.journey_id=:journey_id
              AND e.journey_document_requirement_id=:requirement_ref
              AND e.di_document_id=:document_id
              AND e.association_status='ACTIVE'
              AND upper(jdr.process_area)='BOOKING'
            ORDER BY e.linked_at_utc DESC, e.evidence_id DESC
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "requirement_ref": requirement_ref,
            "document_id": document_id,
        },
    ).mappings().one_or_none()
    if row is None:
        raise AuditCoreError(
            error_code="VAC-VAL-003",
            status_code=400,
            title="Unsupported evidence",
            detail="The selected document is not active evidence for this submitted Booking.",
        )
    return row


@router.post("/cases/{journey_id}/document-review", response_model=TlDocumentReviewResponse)
def submit_tl_document_review(
    tenant_id: str,
    journey_id: UUID,
    payload: TlDocumentReviewCommand,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TlDocumentReviewResponse:
    _authorize_permission(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
        permission_key=_REVIEW_DECIDE_PERMISSION,
    )
    set_tenant_context(connection, tenant_id)
    _require_tl_case_scope(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        journey_id=journey_id,
    )
    _validate_unique_fields(payload.fields)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        if state is None:
            raise NotFoundError(
                error_code="VAC-NF-005",
                title="Booking stage not found",
                detail="Booking stage not found for the submitted case.",
            )
        linked = _linked_document(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirement_ref=payload.requirementRef,
            document_id=payload.documentId,
        )
        evidence_id: UUID = linked["evidence_id"]
        document_type_key = str(linked["document_type_key"] or "").strip().lower()

        _store_fields(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_id=evidence_id,
            document_id=payload.documentId,
            actor_id=human_principal.subject,
            fields=payload.fields,
        )

        next_version = int(state["version_no"]) + 1
        modified_count = sum(field.modifiedValue is not None for field in payload.fields)
        projected_count = 0
        projection_failure_count = 0
        for index, field in enumerate(payload.fields):
            if field.modifiedValue is None:
                continue
            try:
                with connection.begin_nested():
                    projected = _project_known_field(
                        connection,
                        tenant_id=tenant_id,
                        journey_id=journey_id,
                        evidence_id=evidence_id,
                        document_type_key=document_type_key,
                        field=field,
                    )
                if projected is not None:
                    projected_count += 1
            except Exception:
                projection_failure_count += 1

            _append_workflow_event(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                event_type="TL_EXTRACTION_CORRECTED",
                source_kind="HUMAN",
                actor_id=human_principal.subject,
                actor_role_snapshot="TL",
                idempotency_key=f"{idempotency_key}:modified:{index}",
                correlation_id=correlation_id,
                safe_payload={
                    "requirementRef": str(payload.requirementRef),
                    "documentId": str(payload.documentId),
                    "fieldKey": field.fieldKey.strip().lower(),
                    "sourceFactRef": str(field.sourceFactRef),
                    "sourceFactVersion": field.sourceFactVersion,
                },
                aggregate_version=next_version,
            )

        review_event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="TL_DOCUMENT_VERIFIED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot="TL",
            idempotency_key=f"{idempotency_key}:document",
            correlation_id=correlation_id,
            safe_payload={
                "requirementRef": str(payload.requirementRef),
                "documentId": str(payload.documentId),
                "reviewedFieldCount": len(payload.fields),
                "modifiedFieldCount": modified_count,
                "optionalReview": True,
            },
            aggregate_version=next_version,
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET latest_activity_at_utc=now(),
                    updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id
                  AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        return {
            "journeyId": str(journey_id),
            "requirementRef": str(payload.requirementRef),
            "documentId": str(payload.documentId),
            "aggregateVersion": next_version,
            "reviewEventId": str(review_event_id),
            "storedFieldCount": len(payload.fields),
            "modifiedFieldCount": modified_count,
            "projectedFieldCount": projected_count,
            "projectionFailureCount": projection_failure_count,
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.tl.document-review:{journey_id}:{payload.documentId}",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
    )
    return TlDocumentReviewResponse.model_validate(body)


def _responsible_pc_actor(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str:
    actor_id = connection.execute(
        text(
            """
            SELECT e.actor_id
            FROM auditcore.journey_workflow_events e
            WHERE e.tenant_id=:tenant_id
              AND e.journey_id=:journey_id
              AND e.stage_code='BOOKING'
              AND e.event_type='PC_BOOKING_CAPTURE_SUBMITTED'
              AND e.source_kind='HUMAN'
              AND e.actor_id IS NOT NULL
            ORDER BY e.occurred_at_utc DESC, e.event_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    if not actor_id:
        raise AuditCoreError(
            error_code="VAC-CONFLICT-004",
            status_code=409,
            title="Responsible PC is not available",
            detail="A PC re-upload request cannot be assigned because the Booking submitter is not recorded.",
        )
    return str(actor_id)


def _existing_reupload_task(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
):
    return connection.execute(
        text(
            """
            SELECT workflow_task_id,
                   NULLIF(task_payload->>'findingId','')::uuid AS finding_id,
                   assigned_actor_id
            FROM auditcore.workflow_tasks
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND task_type='PC_DOCUMENT_REUPLOAD'
              AND task_status = ANY(:statuses)
              AND task_payload->>'documentId'=:document_id
            ORDER BY created_at_utc DESC, workflow_task_id DESC
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "statuses": list(_ACTIVE_TASK_STATUSES),
            "document_id": str(document_id),
        },
    ).mappings().one_or_none()


def _create_reupload_finding(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
    reason: str,
    correlation_id: str,
) -> UUID:
    finding_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_findings (
                tenant_id, journey_id, finding_type_code, severity,
                finding_status, title, description, created_by_actor_id,
                correlation_id, stage_code, origin_kind, origin_actor_id,
                origin_role_snapshot, rule_key, blocking_completion
            ) VALUES (
                :tenant_id, :journey_id, 'DOCUMENT_EXCEPTION', 'MEDIUM',
                'OPEN', 'Team Lead requested document re-upload', :description,
                :actor_id, :correlation_id, 'BOOKING', 'HUMAN', :actor_id,
                'TL', 'TL_DOCUMENT_REUPLOAD_REQUEST', false
            ) RETURNING audit_finding_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "description": reason.strip(),
            "actor_id": actor_id,
            "correlation_id": correlation_id,
        },
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot, safe_payload,
                correlation_id
            ) VALUES (
                :tenant_id, :finding_id, :journey_id, 'BOOKING',
                'RAISED', :actor_id, 'TL', CAST(:safe_payload AS jsonb),
                :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "journey_id": journey_id,
            "actor_id": actor_id,
            "safe_payload": json.dumps({"reasonCategory": "DOCUMENT_REUPLOAD_REQUEST"}),
            "correlation_id": correlation_id,
        },
    )
    return finding_id


@router.post("/cases/{journey_id}/reupload-request", response_model=TlReuploadRequestResponse)
def request_pc_document_reupload(
    tenant_id: str,
    journey_id: UUID,
    payload: TlReuploadRequestCommand,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TlReuploadRequestResponse:
    _authorize_permission(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
        permission_key=_REVIEW_DECIDE_PERMISSION,
    )
    set_tenant_context(connection, tenant_id)
    case = _require_tl_case_scope(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        journey_id=journey_id,
    )
    assert case is not None
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        _linked_document(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirement_ref=payload.requirementRef,
            document_id=payload.documentId,
        )
        pc_actor_id = _responsible_pc_actor(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        existing = _existing_reupload_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=payload.documentId,
        )
        if existing is not None and existing["finding_id"] is not None:
            return {
                "journeyId": str(journey_id),
                "requirementRef": str(payload.requirementRef),
                "documentId": str(payload.documentId),
                "taskId": str(existing["workflow_task_id"]),
                "findingId": str(existing["finding_id"]),
                "assignedPcActorId": str(existing["assigned_actor_id"] or pc_actor_id),
                "status": "REQUESTED",
            }

        finding_id = _create_reupload_finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            actor_id=human_principal.subject,
            reason=payload.reason,
            correlation_id=correlation_id,
        )
        task_id = create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type="UC03_DOCUMENT_REUPLOAD",
            process_area="BOOKING",
            task_type="PC_DOCUMENT_REUPLOAD",
            assigned_role_code="PC",
            assigned_actor_id=pc_actor_id,
            dealer_id=case["dealer_id"],
            outlet_id=case["outlet_id"],
            task_payload={
                "documentId": str(payload.documentId),
                "requirementRef": str(payload.requirementRef),
                "findingId": str(finding_id),
                "requestedByRole": "TL",
                "reason": payload.reason.strip(),
            },
            effect_key=f"tl-document-reupload:{journey_id}:{payload.documentId}",
            correlation_id=correlation_id,
        )
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        next_version = int(state["version_no"]) + 1 if state is not None else 1
        _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="TL_DOCUMENT_REUPLOAD_REQUESTED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot="TL",
            idempotency_key=f"{idempotency_key}:event",
            correlation_id=correlation_id,
            safe_payload={
                "requirementRef": str(payload.requirementRef),
                "documentId": str(payload.documentId),
                "taskId": str(task_id),
                "findingId": str(finding_id),
                "assignedPcActorId": pc_actor_id,
                "optionalTlReview": True,
            },
            aggregate_version=next_version,
        )
        if state is not None:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.journey_stage_states
                    SET latest_activity_at_utc=now(), updated_at_utc=now(), version_no=:version
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND stage_code='BOOKING'
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
            )
        return {
            "journeyId": str(journey_id),
            "requirementRef": str(payload.requirementRef),
            "documentId": str(payload.documentId),
            "taskId": str(task_id),
            "findingId": str(finding_id),
            "assignedPcActorId": pc_actor_id,
            "status": "REQUESTED",
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.tl.reupload-request:{journey_id}:{payload.documentId}",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
    )
    return TlReuploadRequestResponse.model_validate(body)
