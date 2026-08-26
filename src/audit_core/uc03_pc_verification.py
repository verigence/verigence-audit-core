from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import ConflictError, NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
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
    _resolve_booking_applicability,
    _scope,
    _write_typed_capture,
)
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _parse_if_match,
)

router = APIRouter(tags=["uc03-pc-verification"])
_FAILED_PROCESSING_STATUSES = {"FAILED", "ERROR", "REJECTED"}


class PcBookingSubmitCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any]


class PcVerificationView(BaseModel):
    journeyId: UUID
    captureSubmitted: bool
    pcVerificationStatus: Literal["NOT_SUBMITTED", "PENDING", "VERIFIED"]
    reviewReady: bool
    linkedDocumentCount: int
    pendingDocumentCount: int
    failedDocumentCount: int
    pendingProposalCount: int
    aggregateVersion: int
    captureCompletedAtUtc: datetime | None
    latestActivityAtUtc: datetime


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


def _verification_state(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    for_update: bool = False,
):
    suffix = " FOR UPDATE" if for_update else ""
    row = connection.execute(
        text(
            """
            SELECT journey_id, business_status, capture_completed_at_utc,
                   pc_verification_status, latest_activity_at_utc, version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """ + suffix
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Booking not found",
            detail="Booking stage not found for the requested Project.",
        )
    return row


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


def _view(connection: Connection, *, tenant_id: str, journey_id: UUID) -> PcVerificationView:
    state = _verification_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    readiness = _review_readiness(connection, tenant_id=tenant_id, journey_id=journey_id)
    status = state["pc_verification_status"]
    if state["capture_completed_at_utc"] is None:
        status = "NOT_SUBMITTED"
    elif status is None:
        status = "PENDING"
    return PcVerificationView(
        journeyId=journey_id,
        captureSubmitted=state["capture_completed_at_utc"] is not None,
        pcVerificationStatus=status,
        aggregateVersion=int(state["version_no"]),
        captureCompletedAtUtc=state["capture_completed_at_utc"],
        latestActivityAtUtc=state["latest_activity_at_utc"],
        **readiness,
    )


@router.get(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/pc-verification",
    response_model=PcVerificationView,
)
def get_pc_verification(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> PcVerificationView:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _view(connection, tenant_id=tenant_id, journey_id=journey_id)


@router.post(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/pc-verification/submit",
    response_model=PcVerificationView,
)
def submit_pc_booking_capture(
    tenant_id: str,
    journey_id: UUID,
    payload: PcBookingSubmitCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> PcVerificationView:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _verification_state(connection, tenant_id=tenant_id, journey_id=journey_id, for_update=True)
        if int(state["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Booking version conflict",
                detail="Booking changed since it was loaded. Refresh the Booking and retry.",
            )

        captured_fields: list[str] = []
        for raw_key, value in payload.values.items():
            key = raw_key.strip().upper()
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            _write_typed_capture(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                field_key=key,
                value=value,
                source_evidence_id=None,
            )
            captured_fields.append(key)

        applicability_changes = _resolve_booking_applicability(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        next_version = expected_version + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET capture_completed_at_utc=now(),
                    pc_verification_status='PENDING',
                    latest_activity_at_utc=now(),
                    updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_BOOKING_CAPTURE_SUBMITTED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "pcVerificationStatus": "PENDING",
                "capturedFields": captured_fields,
                "applicabilityChanges": applicability_changes,
                "bookingBusinessStatusChanged": False,
            },
            aggregate_version=next_version,
        )
        return _view(connection, tenant_id=tenant_id, journey_id=journey_id).model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.pc-verification.submit:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version, "values": payload.values},
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return PcVerificationView.model_validate(body)


@router.post(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/pc-verification/verify",
    response_model=PcVerificationView,
)
def verify_pc_booking(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> PcVerificationView:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _verification_state(connection, tenant_id=tenant_id, journey_id=journey_id, for_update=True)
        if int(state["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Booking version conflict",
                detail="Booking changed since it was loaded. Refresh the Booking and retry.",
            )
        if state["capture_completed_at_utc"] is None or state["pc_verification_status"] != "PENDING":
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="PC verification is not pending",
                detail="Submit Booking capture before completing PC verification.",
            )
        readiness = _review_readiness(connection, tenant_id=tenant_id, journey_id=journey_id)
        if not readiness["reviewReady"]:
            raise ConflictError(
                error_code="VAC-CONFLICT-011",
                title="Documents are not ready for review",
                detail="Document Intelligence is still preparing one or more Booking documents.",
            )
        if int(readiness["pendingProposalCount"]) > 0:
            raise ConflictError(
                error_code="VAC-CONFLICT-012",
                title="PC document review is incomplete",
                detail="Review the remaining extracted values before marking the Booking verified.",
            )
        next_version = expected_version + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET pc_verification_status='VERIFIED',
                    latest_activity_at_utc=now(),
                    updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_BOOKING_VERIFIED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "pcVerificationStatus": "VERIFIED",
                "bookingBusinessStatusChanged": False,
                "tlReviewRequired": False,
            },
            aggregate_version=next_version,
        )
        return _view(connection, tenant_id=tenant_id, journey_id=journey_id).model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.pc-verification.verify:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version},
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return PcVerificationView.model_validate(body)


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
