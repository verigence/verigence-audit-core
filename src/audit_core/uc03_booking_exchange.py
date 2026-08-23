from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import ConflictError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _authorize_security,
    _journey_context,
    _parse_if_match,
    _require_expected_version,
    _set_etag,
    _stage_state,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}",
    tags=["uc03-booking-capture"],
)


class ExchangeTakenCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: bool


def _resolve_trade_in_rc(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    exchange_taken: bool,
) -> list[dict[str, str]]:
    rows = connection.execute(
        text(
            """
            SELECT journey_document_requirement_id, requirement_key,
                   condition_snapshot
            FROM auditcore.journey_document_requirements
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND upper(process_area)='BOOKING'
              AND requirement_level='CONDITIONAL'
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    changes: list[dict[str, str]] = []
    for row in rows:
        snapshot = dict(row["condition_snapshot"] or {})
        condition_key = str(snapshot.get("conditionKey") or "").strip().lower()
        if condition_key not in {"exchangetaken", "exchange_taken"}:
            continue
        new_state = "APPLICABLE" if exchange_taken else "NOT_APPLICABLE"
        previous = str(snapshot.get("applicabilityState") or "UNRESOLVED")
        reason = f"exchangeTaken={'Yes' if exchange_taken else 'No'}"
        if previous == new_state and snapshot.get("applicabilityReason") == reason:
            continue
        snapshot["applicabilityState"] = new_state
        snapshot["applicabilityReason"] = reason
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_document_requirements
                SET requirement_status=:requirement_status,
                    condition_snapshot=CAST(:snapshot AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id
                  AND journey_document_requirement_id=:requirement_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "requirement_id": row["journey_document_requirement_id"],
                "requirement_status": "PENDING" if exchange_taken else "NOT_APPLICABLE",
                "snapshot": json.dumps(snapshot),
            },
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_document_assessments
                SET applicability_state=:state,
                    applicability_reason=:reason,
                    answer='UNANSWERED', evidence_id=NULL,
                    answered_at_utc=NULL,
                    version_no=version_no+1,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING' AND requirement_key=:requirement_key
                  AND applicability_state<>:state
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "requirement_key": row["requirement_key"],
                "state": new_state,
                "reason": reason,
            },
        )
        changes.append(
            {
                "requirementKey": row["requirement_key"],
                "previousState": previous,
                "applicabilityState": new_state,
                "reason": reason,
            }
        )
    return changes


@router.put("/capture/EXCHANGE_TAKEN")
def capture_exchange_taken(
    tenant_id: str,
    journey_id: UUID,
    payload: ExchangeTakenCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict:
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

    def execute() -> dict:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_expected_version(state, expected_version)
        if state is None or state["business_status"] not in {
            "BOOKING_STARTED",
            "BOOKING_IN_PROGRESS",
        }:
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Booking state conflict",
                detail="The Booking must be active before capture can change.",
            )

        connection.execute(
            text(
                """
                INSERT INTO auditcore.trade_in_cases (
                    tenant_id, journey_id, details, source_kind
                ) VALUES (
                    :tenant_id, :journey_id,
                    jsonb_build_object('exchangeTaken', :exchange_taken),
                    'OPERATIONAL_INPUT'
                )
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                    details = COALESCE(auditcore.trade_in_cases.details, '{}'::jsonb)
                              || jsonb_build_object('exchangeTaken', :exchange_taken),
                    source_kind='OPERATIONAL_INPUT',
                    updated_at_utc=now(),
                    version_no=auditcore.trade_in_cases.version_no+1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "exchange_taken": payload.value,
            },
        )
        changes = _resolve_trade_in_rc(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            exchange_taken=payload.value,
        )
        next_version = int(state["version_no"]) + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status='BOOKING_IN_PROGRESS',
                    audit_state=CASE WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS' ELSE audit_state END,
                    latest_activity_at_utc=now(), updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="BOOKING_CAPTURE_RECORDED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "fieldKey": "EXCHANGE_TAKEN",
                "owningDomainKey": "TRADE_IN",
                "applicabilityChanges": changes,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "fieldKey": "EXCHANGE_TAKEN",
            "value": payload.value,
            "owningDomainKey": "TRADE_IN",
            "applicabilityChanges": changes,
            "aggregateVersion": next_version,
            "eventId": str(event_id),
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.capture:{journey_id}:EXCHANGE_TAKEN",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version, "value": payload.value},
        execute=execute,
    )
    _set_etag(response, body)
    return body
