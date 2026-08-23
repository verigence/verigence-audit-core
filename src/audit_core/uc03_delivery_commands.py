from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, ConflictError, NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _authorize_security,
    _journey_context,
    _parse_if_match,
    _require_expected_version,
    _set_etag,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/delivery",
    tags=["uc03-delivery"],
)

_BOOKING_INCOMPLETE_RULE = "WF_BOOKING_INCOMPLETE_AT_DELIVERY_START"
_DELIVERY_AUDIT_INCOMPLETE_RULE = "WF_DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE"
_NOT_INTIMATED_RULE = "DL_NOT_INTIMATED"
_VIN_RULE = "DL_VIN_RECONCILIATION"
_DOCUMENT_NO_RULE = "DOC_REQUIRED_ANSWER_NO"
_PAYMENT_UNVERIFIED_RULE = "PAY_UNVERIFIED_RECEIPT"
_VIN_EVALUATOR = "EXACT_COMPARABLE_IDENTIFIER_V1"


class DeliveryCommandResponse(BaseModel):
    journeyId: UUID
    stage: Literal["DELIVERY"] = "DELIVERY"
    businessStatus: str
    auditState: str
    auditStatus: str
    aggregateVersion: int
    latestActivityAtUtc: datetime
    eventId: UUID
    raisedFlagIds: list[UUID] = Field(default_factory=list)


class DeliveryIntimationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal["YES", "NO"]
    reason: str | None = Field(default=None, max_length=4000)
    intimatedAtUtc: datetime | None = None

    @model_validator(mode="after")
    def require_reason_for_no(self):
        if self.answer == "NO" and not (self.reason or "").strip():
            raise ValueError("reason is required when Delivery Intimated is No")
        return self


class DeliveryVehicleObservationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vin: str | None = Field(default=None, max_length=120)
    chassisNumber: str | None = Field(default=None, max_length=120)
    sourceEvidenceId: UUID | None = None

    @model_validator(mode="after")
    def require_identifier(self):
        if not (self.vin or "").strip() and not (self.chassisNumber or "").strip():
            raise ValueError("vin or chassisNumber is required")
        return self


class DeliveryIntimationResponse(BaseModel):
    journeyId: UUID
    answer: Literal["YES", "NO", "UNANSWERED"]
    reason: str | None
    aggregateVersion: int
    eventId: UUID
    flagId: UUID | None = None


class DeliveryVehicleObservationResponse(BaseModel):
    journeyId: UUID
    observedVin: str | None
    observedChassisNumber: str | None
    expectedVin: str | None
    expectedChassisNumber: str | None
    reconciliationStatus: Literal[
        "NOT_EVALUATED", "MATCH", "MISMATCH", "REVIEW_REQUIRED"
    ]
    evaluatorKey: str | None
    aggregateVersion: int
    eventId: UUID
    flagId: UUID | None = None


def _delivery_state(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    for_update: bool = False,
):
    lock_clause = " FOR UPDATE" if for_update else ""
    return connection.execute(
        text(
            """
            SELECT journey_id, business_status, closure_disposition,
                   audit_state, audit_status, first_started_at_utc,
                   business_completed_at_utc, capture_completed_at_utc,
                   latest_activity_at_utc, version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='DELIVERY'
            """
            + lock_clause
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()


def _booking_state(connection: Connection, *, tenant_id: str, journey_id: UUID):
    return connection.execute(
        text(
            """
            SELECT business_status, closure_disposition, audit_state, audit_status,
                   version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()


def _append_delivery_event(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    event_type: str,
    source_kind: Literal["HUMAN", "MACHINE", "SOURCE_SYSTEM"],
    actor_id: str | None,
    actor_role_snapshot: str | None,
    idempotency_key: str | None,
    correlation_id: str | None,
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
                :tenant_id, :journey_id, 'DELIVERY', :event_type, :source_kind,
                :actor_id, :actor_role, :idempotency_key, :correlation_id,
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
            "actor_role": actor_role_snapshot,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "safe_payload": json.dumps(safe_payload, default=str),
            "aggregate_version": aggregate_version,
        },
    ).scalar_one()


def _set_stage_flag_status(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
) -> None:
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET audit_status='FLAGS_RAISED', updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code=:stage_code
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
        },
    )


def _machine_flag(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: Literal["BOOKING", "DELIVERY"],
    rule_key: str,
    finding_type: str,
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"],
    title: str,
    description: str | None,
    correlation_id: str,
    safe_payload: dict[str, Any],
    blocking_completion: bool = False,
) -> UUID:
    existing = connection.execute(
        text(
            """
            SELECT audit_finding_id
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code=:stage_code AND rule_key=:rule_key
              AND finding_status <> 'VOIDED'
            ORDER BY created_at_utc DESC, audit_finding_id DESC
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "rule_key": rule_key,
        },
    ).scalar_one_or_none()
    if existing is not None:
        _set_stage_flag_status(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
        )
        return existing

    finding_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_findings (
                tenant_id, journey_id, finding_type_code, severity,
                finding_status, title, description, created_by_actor_id,
                correlation_id, stage_code, origin_kind, origin_actor_id,
                origin_role_snapshot, rule_key, blocking_completion
            ) VALUES (
                :tenant_id, :journey_id, :finding_type, :severity,
                'OPEN', :title, :description, NULL,
                :correlation_id, :stage_code, 'MACHINE', NULL,
                'SYSTEM', :rule_key, :blocking_completion
            )
            RETURNING audit_finding_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "finding_type": finding_type,
            "severity": severity,
            "title": title,
            "description": description,
            "correlation_id": correlation_id,
            "stage_code": stage_code,
            "rule_key": rule_key,
            "blocking_completion": blocking_completion,
        },
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot,
                safe_payload, correlation_id
            ) VALUES (
                :tenant_id, :finding_id, :journey_id, :stage_code,
                'RAISED', NULL, 'SYSTEM', CAST(:payload AS jsonb),
                :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "payload": json.dumps({"originKind": "MACHINE", "ruleKey": rule_key, **safe_payload}, default=str),
            "correlation_id": correlation_id,
        },
    )
    _set_stage_flag_status(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
    )
    return finding_id


def _booking_outstanding_snapshot(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT jdr.requirement_key, jdr.requirement_status,
                   COALESCE(jda.answer, 'UNANSWERED') AS answer
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN auditcore.journey_document_assessments jda
              ON jda.tenant_id=jdr.tenant_id
             AND jda.journey_id=jdr.journey_id
             AND jda.stage_code='BOOKING'
             AND jda.requirement_key=jdr.requirement_key
            WHERE jdr.tenant_id=:tenant_id AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='BOOKING'
              AND jdr.requirement_status <> 'NOT_APPLICABLE'
              AND (
                    jdr.requirement_status <> 'SATISFIED'
                    OR COALESCE(jda.answer, 'UNANSWERED')='UNANSWERED'
              )
            ORDER BY jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _upsert_delivery_business_record(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    business_status: str,
    actor_id: str,
    completed: bool,
) -> None:
    label = {
        "DELIVERY_STARTED": "Delivery Started",
        "DELIVERY_IN_PROGRESS": "Delivery In Progress",
        "DELIVERY_COMPLETED": "Delivery Completed",
    }[business_status]
    delivery_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.deliveries (
                tenant_id, journey_id, actual_delivery_status_code,
                status_label_snapshot, actual_delivered_at, status_source,
                recorded_by_actor_id
            ) VALUES (
                :tenant_id, :journey_id, :status_code,
                :status_label,
                CASE WHEN :completed THEN now() ELSE NULL END,
                'OPERATIONAL_INPUT', :actor_id
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                actual_delivery_status_code=EXCLUDED.actual_delivery_status_code,
                status_label_snapshot=EXCLUDED.status_label_snapshot,
                actual_delivered_at=CASE
                    WHEN :completed THEN COALESCE(auditcore.deliveries.actual_delivered_at, now())
                    ELSE auditcore.deliveries.actual_delivered_at
                END,
                status_source='OPERATIONAL_INPUT',
                recorded_by_actor_id=:actor_id,
                updated_at_utc=now(),
                version_no=auditcore.deliveries.version_no+1
            RETURNING delivery_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "status_code": business_status,
            "status_label": label,
            "actor_id": actor_id,
            "completed": completed,
        },
    ).scalar_one()
    previous = connection.execute(
        text(
            """
            SELECT actual_delivery_status_code
            FROM auditcore.delivery_status_history
            WHERE tenant_id=:tenant_id AND delivery_id=:delivery_id
            ORDER BY recorded_at_utc DESC, delivery_status_history_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "delivery_id": delivery_id},
    ).scalar_one_or_none()
    if previous == business_status:
        return
    connection.execute(
        text(
            """
            INSERT INTO auditcore.delivery_status_history (
                tenant_id, delivery_id, journey_id,
                actual_delivery_status_code, status_label_snapshot,
                actual_delivered_at, status_source, recorded_by_actor_id
            ) VALUES (
                :tenant_id, :delivery_id, :journey_id,
                :status_code, :status_label,
                CASE WHEN :completed THEN now() ELSE NULL END,
                'OPERATIONAL_INPUT', :actor_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "delivery_id": delivery_id,
            "journey_id": journey_id,
            "status_code": business_status,
            "status_label": label,
            "actor_id": actor_id,
            "completed": completed,
        },
    )


def _delivery_response(row, *, event_id: UUID, flags: list[UUID]) -> dict[str, Any]:
    return DeliveryCommandResponse(
        journeyId=row["journey_id"],
        businessStatus=row["business_status"],
        auditState=row["audit_state"],
        auditStatus=row["audit_status"],
        aggregateVersion=int(row["version_no"]),
        latestActivityAtUtc=row["latest_activity_at_utc"],
        eventId=event_id,
        raisedFlagIds=flags,
    ).model_dump(mode="json")


def _require_delivery_mutable(state) -> None:
    if state is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Delivery has not started",
            detail="Start Delivery before recording Delivery audit work.",
        )


def _material_delivery_activity(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    next_version: int,
) -> None:
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET business_status=CASE
                    WHEN business_status='DELIVERY_STARTED' THEN 'DELIVERY_IN_PROGRESS'
                    ELSE business_status
                END,
                audit_state=CASE
                    WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS'
                    ELSE audit_state
                END,
                latest_activity_at_utc=now(), updated_at_utc=now(),
                version_no=:version
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='DELIVERY'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
    )
    state = _delivery_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    if state is not None and state["business_status"] == "DELIVERY_IN_PROGRESS":
        _upsert_delivery_business_record(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            business_status="DELIVERY_IN_PROGRESS",
            actor_id="SYSTEM",
            completed=False,
        )


def _normalize_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^A-Z0-9]", "", value.upper())
    return normalized or None


def _vin_reconciliation(
    *,
    expected_vin: str | None,
    expected_chassis: str | None,
    observed_vin: str | None,
    observed_chassis: str | None,
) -> str:
    pairs = [
        (_normalize_identifier(expected_vin), _normalize_identifier(observed_vin)),
        (_normalize_identifier(expected_chassis), _normalize_identifier(observed_chassis)),
    ]
    comparable: list[tuple[str, str]] = []
    for expected, observed in pairs:
        if expected and observed and len(expected) == len(observed):
            comparable.append((expected, observed))
    if not comparable:
        return "REVIEW_REQUIRED"
    if any(expected != observed for expected, observed in comparable):
        return "MISMATCH"
    return "MATCH"


def _delivery_audit_gaps(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
) -> tuple[list[str], list[UUID]]:
    gaps: list[str] = []
    flags: list[UUID] = []

    facts = connection.execute(
        text(
            """
            SELECT intimation_answer, non_intimation_reason,
                   vin_reconciliation_status
            FROM auditcore.journey_delivery_audit_facts
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if facts is None or facts["intimation_answer"] == "UNANSWERED":
        gaps.append("DELIVERY_INTIMATION_UNANSWERED")
    elif facts["intimation_answer"] == "NO":
        flags.append(
            _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code="DELIVERY",
                rule_key=_NOT_INTIMATED_RULE,
                finding_type="DELIVERY_NOT_INTIMATED",
                severity="HIGH",
                title="Delivery was not intimated",
                description=facts["non_intimation_reason"],
                correlation_id=correlation_id,
                safe_payload={},
            )
        )
    if facts is not None and facts["vin_reconciliation_status"] == "REVIEW_REQUIRED":
        gaps.append("VIN_RECONCILIATION_REVIEW_REQUIRED")

    documents = connection.execute(
        text(
            """
            SELECT jdr.requirement_key, jdr.requirement_level,
                   jdr.requirement_status,
                   COALESCE(jda.answer, 'UNANSWERED') AS answer
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN auditcore.journey_document_assessments jda
              ON jda.tenant_id=jdr.tenant_id
             AND jda.journey_id=jdr.journey_id
             AND jda.stage_code='DELIVERY'
             AND jda.requirement_key=jdr.requirement_key
            WHERE jdr.tenant_id=:tenant_id AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='DELIVERY'
              AND jdr.requirement_status <> 'NOT_APPLICABLE'
            ORDER BY jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    for document in documents:
        answer = document["answer"]
        if answer == "UNANSWERED":
            gaps.append(f"DOCUMENT_UNANSWERED:{document['requirement_key']}")
        elif answer == "NO":
            flags.append(
                _machine_flag(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code="DELIVERY",
                    rule_key=f"{_DOCUMENT_NO_RULE}:{document['requirement_key']}",
                    finding_type="REQUIRED_DOCUMENT_ANSWER_NO",
                    severity="HIGH" if document["requirement_level"] == "REQUIRED" else "MEDIUM",
                    title="Delivery document answered No",
                    description=f"Requirement {document['requirement_key']} was explicitly answered No.",
                    correlation_id=correlation_id,
                    safe_payload={"requirementKey": document["requirement_key"]},
                )
            )

    unverified_payments = connection.execute(
        text(
            """
            SELECT p.payment_id
            FROM auditcore.payments p
            WHERE p.tenant_id=:tenant_id AND p.journey_id=:journey_id
              AND NOT EXISTS (
                    SELECT 1
                    FROM auditcore.payment_verification_events pve
                    WHERE pve.tenant_id=p.tenant_id
                      AND pve.payment_id=p.payment_id
                      AND pve.verification_result='VERIFIED'
              )
            ORDER BY p.payment_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalars().all()
    if unverified_payments:
        gaps.append("PAYMENT_VERIFICATION_INCOMPLETE")
        flags.append(
            _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code="DELIVERY",
                rule_key=_PAYMENT_UNVERIFIED_RULE,
                finding_type="PAYMENT_UNVERIFIED",
                severity="HIGH",
                title="Delivery payment verification incomplete",
                description="One or more captured payments do not have a VERIFIED realization event.",
                correlation_id=correlation_id,
                safe_payload={"paymentIds": [str(value) for value in unverified_payments]},
                blocking_completion=True,
            )
        )

    return gaps, list(dict.fromkeys(flags))


@router.post("/start", response_model=DeliveryCommandResponse)
def start_delivery(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeliveryCommandResponse:
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
        state = _delivery_state(
            connection, tenant_id=tenant_id, journey_id=journey_id, for_update=True
        )
        _require_expected_version(state, expected_version)
        if state is not None:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Delivery state conflict",
                detail="Delivery has already been started.",
            )
        booking = _booking_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        if booking is None:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking has not started",
                detail="A Booking stage must exist before Delivery can start.",
            )
        if booking["business_status"] in {"BOOKING_CANCELLED", "DUPLICATE_BOOKING"}:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Delivery sequence conflict",
                detail="Delivery cannot start for a cancelled or duplicate Booking.",
            )
        if (
            booking["business_status"] == "BOOKING_CLOSED"
            and booking["closure_disposition"] != "PROCEED_TO_DELIVERY"
        ):
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Delivery sequence conflict",
                detail="This Booking was closed without Delivery.",
            )

        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status,
                    audit_state, audit_status, first_started_at_utc,
                    latest_activity_at_utc, version_no
                ) VALUES (
                    :tenant_id, :journey_id, 'DELIVERY', 'DELIVERY_STARTED',
                    'NOT_STARTED', 'NOT_EVALUATED', now(), now(), 1
                )
                RETURNING journey_id, business_status, audit_state, audit_status,
                          latest_activity_at_utc, version_no
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one()
        _upsert_delivery_business_record(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            business_status="DELIVERY_STARTED",
            actor_id=human_principal.subject,
            completed=False,
        )
        event_id = _append_delivery_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="DELIVERY_STARTED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={"bookingBusinessStatus": booking["business_status"]},
            aggregate_version=1,
        )
        flags: list[UUID] = []
        if not (
            booking["business_status"] == "BOOKING_CLOSED"
            and booking["closure_disposition"] == "PROCEED_TO_DELIVERY"
        ):
            outstanding = _booking_outstanding_snapshot(
                connection, tenant_id=tenant_id, journey_id=journey_id
            )
            flag_id = _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code="BOOKING",
                rule_key=_BOOKING_INCOMPLETE_RULE,
                finding_type="BOOKING_PREREQUISITES_INCOMPLETE_AT_DELIVERY",
                severity="HIGH",
                title="Booking prerequisites incomplete at Delivery Start",
                description="Delivery was started while Booking remained incomplete. Delivery progression was recorded and continues.",
                correlation_id=correlation_id,
                safe_payload={"outstanding": outstanding},
            )
            flags.append(flag_id)
            _append_delivery_event(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                event_type="FLAG_RAISED",
                source_kind="MACHINE",
                actor_id=None,
                actor_role_snapshot="SYSTEM",
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                safe_payload={"findingId": str(flag_id), "ruleKey": _BOOKING_INCOMPLETE_RULE},
                aggregate_version=1,
            )
        refreshed = _delivery_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        return _delivery_response(refreshed, event_id=event_id, flags=flags)

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.delivery.start:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version},
        execute=execute,
    )
    _set_etag(response, body)
    return DeliveryCommandResponse.model_validate(body)


@router.put("/intimation", response_model=DeliveryIntimationResponse)
def record_delivery_intimation(
    tenant_id: str,
    journey_id: UUID,
    payload: DeliveryIntimationCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeliveryIntimationResponse:
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
        state = _delivery_state(
            connection, tenant_id=tenant_id, journey_id=journey_id, for_update=True
        )
        _require_expected_version(state, expected_version)
        _require_delivery_mutable(state)
        next_version = int(state["version_no"]) + 1
        reason = (payload.reason or "").strip() or None
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_delivery_audit_facts (
                    tenant_id, journey_id, intimation_answer,
                    non_intimation_reason, updated_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, :answer, :reason, :actor_id
                )
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                    intimation_answer=EXCLUDED.intimation_answer,
                    non_intimation_reason=EXCLUDED.non_intimation_reason,
                    updated_by_actor_id=EXCLUDED.updated_by_actor_id,
                    version_no=auditcore.journey_delivery_audit_facts.version_no+1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "answer": payload.answer,
                "reason": reason,
                "actor_id": human_principal.subject,
            },
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.deliveries
                SET delivery_intimated_at=CASE
                        WHEN :answer='YES' THEN COALESCE(:intimated_at, delivery_intimated_at, now())
                        ELSE NULL
                    END,
                    updated_at_utc=now(), version_no=version_no+1
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "answer": payload.answer,
                "intimated_at": payload.intimatedAtUtc,
            },
        )
        _material_delivery_activity(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            next_version=next_version,
        )
        flag_id: UUID | None = None
        if payload.answer == "NO":
            flag_id = _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code="DELIVERY",
                rule_key=_NOT_INTIMATED_RULE,
                finding_type="DELIVERY_NOT_INTIMATED",
                severity="HIGH",
                title="Delivery was not intimated",
                description=reason,
                correlation_id=correlation_id,
                safe_payload={},
            )
        event_id = _append_delivery_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="DELIVERY_INTIMATION_RECORDED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={"answer": payload.answer, "hasReason": reason is not None},
            aggregate_version=next_version,
        )
        return DeliveryIntimationResponse(
            journeyId=journey_id,
            answer=payload.answer,
            reason=reason,
            aggregateVersion=next_version,
            eventId=event_id,
            flagId=flag_id,
        ).model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.delivery.intimation:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version, **payload.model_dump(mode="json")},
        execute=execute,
    )
    _set_etag(response, body)
    return DeliveryIntimationResponse.model_validate(body)


@router.put("/vehicle-observation", response_model=DeliveryVehicleObservationResponse)
def record_delivery_vehicle_observation(
    tenant_id: str,
    journey_id: UUID,
    payload: DeliveryVehicleObservationCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeliveryVehicleObservationResponse:
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
        state = _delivery_state(
            connection, tenant_id=tenant_id, journey_id=journey_id, for_update=True
        )
        _require_expected_version(state, expected_version)
        _require_delivery_mutable(state)
        if payload.sourceEvidenceId is not None:
            exists = connection.execute(
                text(
                    """
                    SELECT 1 FROM auditcore.evidence
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                      AND evidence_id=:evidence_id AND association_status='ACTIVE'
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "evidence_id": payload.sourceEvidenceId,
                },
            ).scalar_one_or_none()
            if exists is None:
                raise AuditCoreError(
                    error_code="VAC-VAL-003",
                    status_code=400,
                    title="Unsupported evidence",
                    detail="The VIN/photo evidence is not linked to this Delivery.",
                )
        expected = connection.execute(
            text(
                """
                SELECT vin, chassis_number
                FROM auditcore.vehicle_records
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one_or_none()
        expected_vin = expected["vin"] if expected else None
        expected_chassis = expected["chassis_number"] if expected else None
        status = _vin_reconciliation(
            expected_vin=expected_vin,
            expected_chassis=expected_chassis,
            observed_vin=payload.vin,
            observed_chassis=payload.chassisNumber,
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_delivery_audit_facts (
                    tenant_id, journey_id, observed_vin,
                    observed_chassis_number, observed_source_evidence_id,
                    vin_reconciliation_status, vin_evaluator_key,
                    vin_evaluated_at_utc, updated_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, :vin, :chassis, :evidence_id,
                    :status, :evaluator, now(), :actor_id
                )
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                    observed_vin=EXCLUDED.observed_vin,
                    observed_chassis_number=EXCLUDED.observed_chassis_number,
                    observed_source_evidence_id=EXCLUDED.observed_source_evidence_id,
                    vin_reconciliation_status=EXCLUDED.vin_reconciliation_status,
                    vin_evaluator_key=EXCLUDED.vin_evaluator_key,
                    vin_evaluated_at_utc=EXCLUDED.vin_evaluated_at_utc,
                    updated_by_actor_id=EXCLUDED.updated_by_actor_id,
                    version_no=auditcore.journey_delivery_audit_facts.version_no+1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "vin": (payload.vin or "").strip() or None,
                "chassis": (payload.chassisNumber or "").strip() or None,
                "evidence_id": payload.sourceEvidenceId,
                "status": status,
                "evaluator": _VIN_EVALUATOR,
                "actor_id": human_principal.subject,
            },
        )
        next_version = int(state["version_no"]) + 1
        _material_delivery_activity(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            next_version=next_version,
        )
        flag_id: UUID | None = None
        if status == "MISMATCH":
            flag_id = _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code="DELIVERY",
                rule_key=_VIN_RULE,
                finding_type="VIN_RECONCILIATION_MISMATCH",
                severity="CRITICAL",
                title="VIN/chassis reconciliation mismatch",
                description="Comparable full vehicle identifiers conflict. Physical Delivery progression remains recordable.",
                correlation_id=correlation_id,
                safe_payload={"evaluatorKey": _VIN_EVALUATOR},
            )
        event_id = _append_delivery_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="DELIVERY_VEHICLE_OBSERVATION_RECORDED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "reconciliationStatus": status,
                "sourceEvidenceId": str(payload.sourceEvidenceId) if payload.sourceEvidenceId else None,
            },
            aggregate_version=next_version,
        )
        return DeliveryVehicleObservationResponse(
            journeyId=journey_id,
            observedVin=(payload.vin or "").strip() or None,
            observedChassisNumber=(payload.chassisNumber or "").strip() or None,
            expectedVin=expected_vin,
            expectedChassisNumber=expected_chassis,
            reconciliationStatus=status,
            evaluatorKey=_VIN_EVALUATOR,
            aggregateVersion=next_version,
            eventId=event_id,
            flagId=flag_id,
        ).model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.delivery.vehicle-observation:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version, **payload.model_dump(mode="json")},
        execute=execute,
    )
    _set_etag(response, body)
    return DeliveryVehicleObservationResponse.model_validate(body)


@router.post("/complete", response_model=DeliveryCommandResponse)
def complete_delivery(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeliveryCommandResponse:
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
        state = _delivery_state(
            connection, tenant_id=tenant_id, journey_id=journey_id, for_update=True
        )
        _require_expected_version(state, expected_version)
        _require_delivery_mutable(state)
        if state["business_status"] == "DELIVERY_COMPLETED":
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Delivery state conflict",
                detail="Delivery has already been completed.",
            )
        if state["business_status"] not in {"DELIVERY_STARTED", "DELIVERY_IN_PROGRESS"}:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Delivery state conflict",
                detail="The current Delivery state cannot be completed.",
            )

        gaps, flags = _delivery_audit_gaps(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            correlation_id=correlation_id,
        )
        if gaps:
            gap_flag = _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code="DELIVERY",
                rule_key=_DELIVERY_AUDIT_INCOMPLETE_RULE,
                finding_type="DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE",
                severity="HIGH",
                title="Delivery completed with audit work outstanding",
                description="Physical Delivery was completed and recorded while configured audit work remains outstanding.",
                correlation_id=correlation_id,
                safe_payload={"gaps": gaps},
            )
            flags.append(gap_flag)
        flags = list(dict.fromkeys(flags))
        next_version = int(state["version_no"]) + 1
        existing_flags = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='DELIVERY' AND finding_status <> 'VOIDED'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        audit_status = "FLAGS_RAISED" if existing_flags else "NO_FLAGS"
        audit_state = "IN_PROGRESS" if gaps else "COMPLETE"
        row = connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status='DELIVERY_COMPLETED',
                    audit_state=CAST(:audit_state AS varchar),
                    audit_status=CASE
                        WHEN audit_status='FLAGS_RAISED' THEN 'FLAGS_RAISED'
                        ELSE CAST(:audit_status AS varchar)
                    END,
                    business_completed_at_utc=COALESCE(business_completed_at_utc, now()),
                    capture_completed_at_utc=CASE
                        WHEN CAST(:audit_state AS varchar)='COMPLETE' THEN COALESCE(capture_completed_at_utc, now())
                        ELSE capture_completed_at_utc
                    END,
                    latest_activity_at_utc=now(), updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='DELIVERY'
                RETURNING journey_id, business_status, audit_state, audit_status,
                          latest_activity_at_utc, version_no
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "audit_state": audit_state,
                "audit_status": audit_status,
                "version": next_version,
            },
        ).mappings().one()
        _upsert_delivery_business_record(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            business_status="DELIVERY_COMPLETED",
            actor_id=human_principal.subject,
            completed=True,
        )
        event_id = _append_delivery_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="DELIVERY_COMPLETED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={"auditGaps": gaps, "raisedFlagIds": [str(value) for value in flags]},
            aggregate_version=next_version,
        )
        return _delivery_response(row, event_id=event_id, flags=flags)

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.delivery.complete:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version},
        execute=execute,
    )
    _set_etag(response, body)
    return DeliveryCommandResponse.model_validate(body)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


@router.get("/workspace")
def get_delivery_workspace(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
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
    delivery = _delivery_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    if delivery is None:
        raise NotFoundError(
            error_code="VAC-NF-012",
            title="Delivery not started",
            detail="Delivery workspace is available after Delivery has started.",
        )
    booking = _booking_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    facts = connection.execute(
        text(
            """
            SELECT intimation_answer, non_intimation_reason, observed_vin,
                   observed_chassis_number, observed_source_evidence_id,
                   vin_reconciliation_status, vin_evaluator_key,
                   vin_evaluated_at_utc
            FROM auditcore.journey_delivery_audit_facts
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    expected_vehicle = connection.execute(
        text(
            """
            SELECT vin, chassis_number, source_evidence_id
            FROM auditcore.vehicle_records
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    documents = connection.execute(
        text(
            """
            SELECT jdr.requirement_key, jdr.document_type_key,
                   jdr.requirement_level, jdr.requirement_status,
                   jdr.condition_snapshot,
                   COALESCE(jda.answer, 'UNANSWERED') AS answer,
                   jda.evidence_id, jda.remarks,
                   jda.applicability_state, jda.applicability_reason
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN auditcore.journey_document_assessments jda
              ON jda.tenant_id=jdr.tenant_id
             AND jda.journey_id=jdr.journey_id
             AND jda.stage_code='DELIVERY'
             AND jda.requirement_key=jdr.requirement_key
            WHERE jdr.tenant_id=:tenant_id AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='DELIVERY'
            ORDER BY jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    payments = connection.execute(
        text(
            """
            SELECT p.payment_id, p.payment_at_utc, p.amount, p.currency_code,
                   p.payment_method_code, p.payment_reference,
                   latest.verification_result, latest.verification_notes,
                   latest.occurred_at_utc AS verification_at_utc
            FROM auditcore.payments p
            LEFT JOIN LATERAL (
                SELECT pve.verification_result, pve.verification_notes,
                       pve.occurred_at_utc
                FROM auditcore.payment_verification_events pve
                WHERE pve.tenant_id=p.tenant_id AND pve.payment_id=p.payment_id
                ORDER BY pve.occurred_at_utc DESC, pve.payment_verification_event_id DESC
                LIMIT 1
            ) latest ON true
            WHERE p.tenant_id=:tenant_id AND p.journey_id=:journey_id
            ORDER BY p.created_at_utc, p.payment_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    flags = connection.execute(
        text(
            """
            SELECT audit_finding_id, rule_key, finding_type_code, severity,
                   finding_status, title, description, blocking_completion,
                   created_at_utc
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code IN ('BOOKING','DELIVERY')
            ORDER BY created_at_utc DESC, audit_finding_id DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    booking_incomplete = not (
        booking is not None
        and booking["business_status"] == "BOOKING_CLOSED"
        and booking["closure_disposition"] == "PROCEED_TO_DELIVERY"
    )
    return {
        "journeyId": str(journey_id),
        "operatingRole": context["operating_role"],
        "delivery": {
            "businessStatus": delivery["business_status"],
            "auditState": delivery["audit_state"],
            "auditStatus": delivery["audit_status"],
            "aggregateVersion": int(delivery["version_no"]),
            "startedAtUtc": _iso(delivery["first_started_at_utc"]),
            "completedAtUtc": _iso(delivery["business_completed_at_utc"]),
        },
        "booking": {
            "businessStatus": booking["business_status"] if booking else None,
            "closureDisposition": booking["closure_disposition"] if booking else None,
            "incompleteAtDelivery": booking_incomplete,
            "warning": (
                "Booking audit/capture remains incomplete. Delivery can continue; the exception is flagged."
                if booking_incomplete
                else None
            ),
        },
        "intimation": {
            "answer": facts["intimation_answer"] if facts else "UNANSWERED",
            "reason": facts["non_intimation_reason"] if facts else None,
        },
        "vehicle": {
            "expectedVin": expected_vehicle["vin"] if expected_vehicle else None,
            "expectedChassisNumber": expected_vehicle["chassis_number"] if expected_vehicle else None,
            "observedVin": facts["observed_vin"] if facts else None,
            "observedChassisNumber": facts["observed_chassis_number"] if facts else None,
            "observedSourceEvidenceId": str(facts["observed_source_evidence_id"]) if facts and facts["observed_source_evidence_id"] else None,
            "reconciliationStatus": facts["vin_reconciliation_status"] if facts else "NOT_EVALUATED",
            "evaluatorKey": facts["vin_evaluator_key"] if facts else None,
            "evaluatedAtUtc": _iso(facts["vin_evaluated_at_utc"]) if facts else None,
        },
        "documents": [
            {
                "requirementKey": row["requirement_key"],
                "documentTypeKey": row["document_type_key"],
                "requirementLevel": row["requirement_level"],
                "requirementStatus": row["requirement_status"],
                "answer": row["answer"],
                "evidenceId": str(row["evidence_id"]) if row["evidence_id"] else None,
                "remarks": row["remarks"],
                "applicabilityState": row["applicability_state"] or (
                    "NOT_APPLICABLE" if row["requirement_status"] == "NOT_APPLICABLE" else "APPLICABLE"
                ),
                "applicabilityReason": row["applicability_reason"],
            }
            for row in documents
        ],
        "payments": [
            {
                "paymentId": str(row["payment_id"]),
                "paymentAtUtc": _iso(row["payment_at_utc"]),
                "amount": str(row["amount"]),
                "currencyCode": row["currency_code"],
                "paymentMethodCode": row["payment_method_code"],
                "paymentReference": row["payment_reference"],
                "verificationResult": row["verification_result"],
                "verificationNotes": row["verification_notes"],
                "verificationAtUtc": _iso(row["verification_at_utc"]),
            }
            for row in payments
        ],
        "flags": [
            {
                "flagId": str(row["audit_finding_id"]),
                "ruleKey": row["rule_key"],
                "type": row["finding_type_code"],
                "severity": row["severity"],
                "status": row["finding_status"],
                "title": row["title"],
                "description": row["description"],
                "blockingCompletion": bool(row["blocking_completion"]),
                "createdAtUtc": _iso(row["created_at_utc"]),
            }
            for row in flags
        ],
    }
