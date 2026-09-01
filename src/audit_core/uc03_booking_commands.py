from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import (
    AuditCoreError,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
)
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)
from audit_core.telemetry import trace_span

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/booking",
    tags=["uc03-booking"],
)

_PERMISSION_KEY = "audit.journey.update"
_DEFAULT_CLOSE_NO_DELIVERY_REASONS = {
    "FINANCE_NOT_APPROVED",
    "VEHICLE_UNAVAILABLE",
    "CUSTOMER_SHIFTED_DEALER",
    "OTHER",
}
_DEFAULT_CANCEL_REASONS = {"CUSTOMER_CANCELLED", "DEALER_CANCELLED"}
_DUPLICATE_RULE_KEY = "WF_DUPLICATE_BOOKING"


class BookingReasonCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    closeReasonCode: str = Field(min_length=1, max_length=100)
    remarks: str | None = Field(default=None, max_length=4000)


class DuplicateBookingCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remarks: str | None = Field(default=None, max_length=4000)


class BookingCommandResponse(BaseModel):
    journeyId: UUID
    stage: Literal["BOOKING"] = "BOOKING"
    businessStatus: str
    closureDisposition: str | None
    auditState: str
    auditStatus: str
    closeReasonCode: str | None
    closureRemarks: str | None
    aggregateVersion: int
    latestActivityAtUtc: datetime
    eventId: UUID
    flagId: UUID | None = None


def _authorize_security(
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
            detail="Booking work is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


def _journey_context(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
) -> dict[str, Any]:
    journey = connection.execute(
        text(
            """
            SELECT j.dealer_id, j.outlet_id, j.policy_version_id,
                   ppv.policy_settings
            FROM auditcore.journeys j
            LEFT JOIN auditcore.project_policy_versions ppv
              ON ppv.tenant_id = j.tenant_id
             AND ppv.policy_version_id = j.policy_version_id
            WHERE j.tenant_id = :tenant_id
              AND j.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if journey is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Booking not found",
            detail="Booking case not found for the requested Project.",
        )

    roles = connection.execute(
        text(
            """
            SELECT array_agg(DISTINCT ba.business_role_code ORDER BY ba.business_role_code)
            FROM auditcore.business_assignments ba
            WHERE ba.tenant_id = :tenant_id
              AND ba.security_actor_id = :actor_id
              AND ba.assignment_status = 'ACTIVE'
              AND ba.effective_from <= now()
              AND (ba.effective_to IS NULL OR ba.effective_to >= now())
              AND (
                    ba.dealer_id IS NULL
                    OR (
                        ba.dealer_id = :dealer_id
                        AND (ba.outlet_id IS NULL OR ba.outlet_id = :outlet_id)
                    )
              )
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "dealer_id": journey["dealer_id"],
            "outlet_id": journey["outlet_id"],
        },
    ).scalar_one()
    if not roles:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )
    if len(roles) != 1:
        raise ConflictError(
            error_code="VAC-CONFLICT-006",
            title="Ambiguous Project operating role",
            detail="The current Project assignments resolve to more than one operating role.",
        )

    result = dict(journey)
    result["operating_role"] = roles[0]
    return result


def _parse_if_match(value: str) -> int:
    candidate = value.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:].strip()
    if len(candidate) >= 2 and candidate[0] == '"' and candidate[-1] == '"':
        candidate = candidate[1:-1]
    try:
        version = int(candidate)
    except ValueError as exc:
        raise AuditCoreError(
            error_code="VAC-VAL-001",
            status_code=400,
            title="Validation failed",
            detail="If-Match must contain the expected Booking aggregate version.",
        ) from exc
    if version < 0:
        raise AuditCoreError(
            error_code="VAC-VAL-001",
            status_code=400,
            title="Validation failed",
            detail="If-Match Booking aggregate version cannot be negative.",
        )
    return version


def _aggregate_lock(connection: Connection, *, tenant_id: str, journey_id: UUID) -> None:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"uc03-booking:{tenant_id}:{journey_id}"},
    )


def _stage_state(connection: Connection, *, tenant_id: str, journey_id: UUID):
    return connection.execute(
        text(
            """
            SELECT business_status, closure_disposition, audit_state, audit_status,
                   close_reason_code, closure_remarks, latest_activity_at_utc, version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND stage_code = 'BOOKING'
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()


def _require_expected_version(state, expected_version: int) -> None:
    current_version = int(state["version_no"]) if state is not None else 0
    if current_version != expected_version:
        raise ConflictError(
            error_code="VAC-CONFLICT-005",
            title="Booking version conflict",
            detail=(
                "Booking changed since it was loaded. Refresh the Booking and retry the action."
            ),
        )


def _require_transition(state, *, allowed: set[str], action: str) -> None:
    if state is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking has not started",
            detail=f"Booking must be started before it can be {action}.",
        )
    if state["business_status"] not in allowed:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking state conflict",
            detail=f"The current Booking state does not allow it to be {action}.",
        )


def _booking_policy_settings(context: dict[str, Any]) -> dict[str, Any]:
    settings = context.get("policy_settings")
    if not isinstance(settings, dict):
        return {}
    uc03 = settings.get("uc03")
    if not isinstance(uc03, dict):
        return {}
    booking = uc03.get("booking")
    return booking if isinstance(booking, dict) else {}


def _reason_codes(
    context: dict[str, Any],
    *,
    config_key: str,
    defaults: set[str],
) -> set[str]:
    configured = _booking_policy_settings(context).get(config_key)
    if not isinstance(configured, list):
        return set(defaults)
    result: set[str] = set()
    for item in configured:
        if isinstance(item, str) and item.strip():
            result.add(item.strip().upper())
        elif isinstance(item, dict):
            code = item.get("code")
            if isinstance(code, str) and code.strip():
                result.add(code.strip().upper())
    return result or set(defaults)


def _validated_reason(
    context: dict[str, Any],
    *,
    config_key: str,
    defaults: set[str],
    reason_code: str,
    remarks: str | None,
) -> str:
    normalized = reason_code.strip().upper()
    if normalized not in _reason_codes(context, config_key=config_key, defaults=defaults):
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail="The selected Booking reason is not enabled for this Project.",
        )
    if normalized == "OTHER" and not (remarks or "").strip():
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail="Remarks are required when the Booking reason is Other.",
        )
    return normalized


def _append_workflow_event(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    event_type: str,
    source_kind: Literal["HUMAN", "MACHINE", "SOURCE_SYSTEM"],
    actor_id: str | None,
    actor_role_snapshot: str | None,
    idempotency_key: str,
    correlation_id: str,
    safe_payload: dict[str, Any],
    aggregate_version: int,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_workflow_events (
                tenant_id, journey_id, stage_code, event_type, source_kind,
                actor_id, actor_role_snapshot, idempotency_key, correlation_id,
                safe_payload, occurred_at_utc, aggregate_version
            ) VALUES (
                :tenant_id, :journey_id, 'BOOKING', :event_type, :source_kind,
                :actor_id, :actor_role_snapshot, :idempotency_key, :correlation_id,
                CAST(:safe_payload AS jsonb), now(), :aggregate_version
            )
            RETURNING event_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "event_type": event_type,
            "source_kind": source_kind,
            "actor_id": actor_id,
            "actor_role_snapshot": actor_role_snapshot,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "safe_payload": json.dumps(safe_payload),
            "aggregate_version": aggregate_version,
        },
    ).scalar_one()


def _build_response(row, *, event_id: UUID, flag_id: UUID | None = None) -> dict[str, Any]:
    return BookingCommandResponse(
        journeyId=row["journey_id"],
        businessStatus=row["business_status"],
        closureDisposition=row["closure_disposition"],
        auditState=row["audit_state"],
        auditStatus=row["audit_status"],
        closeReasonCode=row["close_reason_code"],
        closureRemarks=row["closure_remarks"],
        aggregateVersion=row["version_no"],
        latestActivityAtUtc=row["latest_activity_at_utc"],
        eventId=event_id,
        flagId=flag_id,
    ).model_dump(mode="json")


def _set_etag(response: Response, body: dict[str, Any]) -> None:
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'


def _insert_duplicate_flag(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
    actor_role: str,
    remarks: str | None,
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
                :tenant_id, :journey_id, 'DUPLICATE_BOOKING', 'HIGH',
                'OPEN', 'Duplicate Booking', :description, :created_by_actor_id,
                :correlation_id, 'BOOKING', 'MACHINE', NULL,
                'SYSTEM', :rule_key, false
            )
            RETURNING audit_finding_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "description": (remarks or "").strip() or None,
            "created_by_actor_id": actor_id,
            "correlation_id": correlation_id,
            "rule_key": _DUPLICATE_RULE_KEY,
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
                'RAISED', :actor_id, :actor_role, CAST(:safe_payload AS jsonb),
                :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "journey_id": journey_id,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "safe_payload": json.dumps(
                {"originKind": "MACHINE", "ruleKey": _DUPLICATE_RULE_KEY}
            ),
            "correlation_id": correlation_id,
        },
    )
    return finding_id


@router.post("/start", response_model=BookingCommandResponse)
def start_booking(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingCommandResponse:
    with trace_span("booking.authorize", correlation_id=get_correlation_id(request)):
        _authorize_security(
            authorization_client,
            human_principal=human_principal,
            tenant_id=tenant_id,
        )
    set_tenant_context(connection, tenant_id)
    context = _journey_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_expected_version(state, expected_version)
        if state is not None:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking state conflict",
                detail="Booking has already been started.",
            )
        row = connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status,
                    audit_state, audit_status, first_started_at_utc,
                    latest_activity_at_utc, version_no
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', 'BOOKING_STARTED',
                    'NOT_STARTED', 'NOT_EVALUATED', now(), now(), 1
                )
                RETURNING journey_id, business_status, closure_disposition,
                          audit_state, audit_status, close_reason_code,
                          closure_remarks, latest_activity_at_utc, version_no
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one()
        event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="BOOKING_STARTED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={},
            aggregate_version=1,
        )
        return _build_response(row, event_id=event_id)

    with trace_span("booking.execute", correlation_id=correlation_id):
        body, _ = execute_idempotent_json_command(
            connection,
            tenant_id=tenant_id,
            operation_key=f"uc03.booking.start:{journey_id}",
            idempotency_key=idempotency_key,
            request_payload={"expectedVersion": expected_version},
            execute=execute,
        )
    _set_etag(response, body)
    return BookingCommandResponse.model_validate(body)


@router.post("/close-no-delivery", response_model=BookingCommandResponse)
def close_booking_no_delivery(
    tenant_id: str,
    journey_id: UUID,
    payload: BookingReasonCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingCommandResponse:
    with trace_span("booking.authorize", correlation_id=get_correlation_id(request)):
        _authorize_security(
            authorization_client,
            human_principal=human_principal,
            tenant_id=tenant_id,
        )
    set_tenant_context(connection, tenant_id)
    context = _journey_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )
    expected_version = _parse_if_match(if_match)
    reason_code = _validated_reason(
        context,
        config_key="closeNoDeliveryReasons",
        defaults=_DEFAULT_CLOSE_NO_DELIVERY_REASONS,
        reason_code=payload.closeReasonCode,
        remarks=payload.remarks,
    )
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_expected_version(state, expected_version)
        _require_transition(
            state,
            allowed={"BOOKING_STARTED", "BOOKING_IN_PROGRESS"},
            action="closed with no Delivery",
        )
        next_version = int(state["version_no"]) + 1
        row = connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status = 'BOOKING_CLOSED',
                    closure_disposition = 'NO_DELIVERY',
                    close_reason_code = :reason_code,
                    closure_remarks = :remarks,
                    closed_by_actor_id = :actor_id,
                    closed_at_utc = now(),
                    business_completed_at_utc = now(),
                    latest_activity_at_utc = now(),
                    updated_at_utc = now(),
                    version_no = :next_version
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND stage_code = 'BOOKING'
                RETURNING journey_id, business_status, closure_disposition,
                          audit_state, audit_status, close_reason_code,
                          closure_remarks, latest_activity_at_utc, version_no
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "reason_code": reason_code,
                "remarks": (payload.remarks or "").strip() or None,
                "actor_id": human_principal.subject,
                "next_version": next_version,
            },
        ).mappings().one()
        event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="BOOKING_CLOSED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "closureDisposition": "NO_DELIVERY",
                "closeReasonCode": reason_code,
            },
            aggregate_version=next_version,
        )
        return _build_response(row, event_id=event_id)

    with trace_span("booking.execute", correlation_id=correlation_id):
        body, _ = execute_idempotent_json_command(
            connection,
            tenant_id=tenant_id,
            operation_key=f"uc03.booking.close-no-delivery:{journey_id}",
            idempotency_key=idempotency_key,
            request_payload={
                "expectedVersion": expected_version,
                **payload.model_dump(mode="json"),
                "closeReasonCode": reason_code,
            },
            execute=execute,
        )
    _set_etag(response, body)
    return BookingCommandResponse.model_validate(body)


@router.post("/cancel", response_model=BookingCommandResponse)
def cancel_booking(
    tenant_id: str,
    journey_id: UUID,
    payload: BookingReasonCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingCommandResponse:
    with trace_span("booking.authorize", correlation_id=get_correlation_id(request)):
        _authorize_security(
            authorization_client,
            human_principal=human_principal,
            tenant_id=tenant_id,
        )
    set_tenant_context(connection, tenant_id)
    context = _journey_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )
    expected_version = _parse_if_match(if_match)
    reason_code = _validated_reason(
        context,
        config_key="cancelReasons",
        defaults=_DEFAULT_CANCEL_REASONS,
        reason_code=payload.closeReasonCode,
        remarks=payload.remarks,
    )
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_expected_version(state, expected_version)
        _require_transition(
            state,
            allowed={"BOOKING_STARTED", "BOOKING_IN_PROGRESS"},
            action="cancelled",
        )
        next_version = int(state["version_no"]) + 1
        row = connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status = 'BOOKING_CANCELLED',
                    closure_disposition = NULL,
                    close_reason_code = :reason_code,
                    closure_remarks = :remarks,
                    closed_by_actor_id = :actor_id,
                    closed_at_utc = now(),
                    business_completed_at_utc = now(),
                    latest_activity_at_utc = now(),
                    updated_at_utc = now(),
                    version_no = :next_version
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND stage_code = 'BOOKING'
                RETURNING journey_id, business_status, closure_disposition,
                          audit_state, audit_status, close_reason_code,
                          closure_remarks, latest_activity_at_utc, version_no
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "reason_code": reason_code,
                "remarks": (payload.remarks or "").strip() or None,
                "actor_id": human_principal.subject,
                "next_version": next_version,
            },
        ).mappings().one()
        event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="BOOKING_CANCELLED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={"closeReasonCode": reason_code},
            aggregate_version=next_version,
        )
        return _build_response(row, event_id=event_id)

    with trace_span("booking.execute", correlation_id=correlation_id):
        body, _ = execute_idempotent_json_command(
            connection,
            tenant_id=tenant_id,
            operation_key=f"uc03.booking.cancel:{journey_id}",
            idempotency_key=idempotency_key,
            request_payload={
                "expectedVersion": expected_version,
                **payload.model_dump(mode="json"),
                "closeReasonCode": reason_code,
            },
            execute=execute,
        )
    _set_etag(response, body)
    return BookingCommandResponse.model_validate(body)


@router.post("/mark-duplicate", response_model=BookingCommandResponse)
def mark_duplicate_booking(
    tenant_id: str,
    journey_id: UUID,
    payload: DuplicateBookingCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingCommandResponse:
    with trace_span("booking.authorize", correlation_id=get_correlation_id(request)):
        _authorize_security(
            authorization_client,
            human_principal=human_principal,
            tenant_id=tenant_id,
        )
    set_tenant_context(connection, tenant_id)
    context = _journey_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_expected_version(state, expected_version)
        _require_transition(
            state,
            allowed={"BOOKING_STARTED", "BOOKING_IN_PROGRESS"},
            action="marked duplicate",
        )
        next_version = int(state["version_no"]) + 1
        row = connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status = 'DUPLICATE_BOOKING',
                    closure_disposition = NULL,
                    close_reason_code = 'DUPLICATE_BOOKING',
                    closure_remarks = :remarks,
                    closed_by_actor_id = :actor_id,
                    closed_at_utc = now(),
                    business_completed_at_utc = now(),
                    audit_status = 'FLAGS_RAISED',
                    latest_activity_at_utc = now(),
                    updated_at_utc = now(),
                    version_no = :next_version
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND stage_code = 'BOOKING'
                RETURNING journey_id, business_status, closure_disposition,
                          audit_state, audit_status, close_reason_code,
                          closure_remarks, latest_activity_at_utc, version_no
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "remarks": (payload.remarks or "").strip() or None,
                "actor_id": human_principal.subject,
                "next_version": next_version,
            },
        ).mappings().one()
        event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="BOOKING_MARKED_DUPLICATE",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={},
            aggregate_version=next_version,
        )
        flag_id = _insert_duplicate_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            actor_id=human_principal.subject,
            actor_role=context["operating_role"],
            remarks=payload.remarks,
            correlation_id=correlation_id,
        )
        _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="FLAG_RAISED",
            source_kind="MACHINE",
            actor_id=None,
            actor_role_snapshot="SYSTEM",
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "findingId": str(flag_id),
                "ruleKey": _DUPLICATE_RULE_KEY,
            },
            aggregate_version=next_version,
        )
        return _build_response(row, event_id=event_id, flag_id=flag_id)

    with trace_span("booking.execute", correlation_id=correlation_id):
        body, _ = execute_idempotent_json_command(
            connection,
            tenant_id=tenant_id,
            operation_key=f"uc03.booking.mark-duplicate:{journey_id}",
            idempotency_key=idempotency_key,
            request_payload={
                "expectedVersion": expected_version,
                **payload.model_dump(mode="json"),
            },
            execute=execute,
        )
    _set_etag(response, body)
    return BookingCommandResponse.model_validate(body)
