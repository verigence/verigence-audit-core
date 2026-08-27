from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, ConflictError
from audit_core.idempotency import stable_request_hash
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_commands import _authorize_security

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-create-booking"])

_OPERATION_KEY = "uc03.booking.create"


class CreateBookingCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outletId: UUID
    customerName: str = Field(min_length=1, max_length=200)


class CreateBookingResponse(BaseModel):
    journeyId: UUID
    customerId: UUID
    dealerId: UUID
    outletId: UUID
    businessStatus: str
    aggregateVersion: int


def _selected_pc_outlet(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    outlet_id: UUID,
) -> dict:
    """Validate only the already-selected working Outlet for this PC."""

    row = connection.execute(
        text(
            """
            SELECT
                o.outlet_id,
                o.outlet_name,
                o.outlet_classification,
                d.dealer_id,
                d.dealer_name
            FROM auditcore.dealer_outlets o
            JOIN auditcore.dealers d
              ON d.tenant_id = o.tenant_id
             AND d.dealer_id = o.dealer_id
            WHERE o.tenant_id = :tenant_id
              AND o.outlet_id = :outlet_id
              AND o.status = 'ACTIVE'
              AND d.status = 'ACTIVE'
              AND EXISTS (
                  SELECT 1
                  FROM auditcore.business_assignments ba
                  WHERE ba.tenant_id = o.tenant_id
                    AND ba.security_actor_id = :actor_id
                    AND ba.business_role_code = 'PC'
                    AND ba.assignment_status = 'ACTIVE'
                    AND ba.effective_from <= now()
                    AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                    AND ba.dealer_id = o.dealer_id
                    AND ba.outlet_id = o.outlet_id
              )
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "outlet_id": outlet_id,
        },
    ).mappings().one_or_none()
    if row is None:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )
    return dict(row)


def _effective_project_versions(
    connection: Connection,
    *,
    tenant_id: str,
) -> dict[str, UUID | None]:
    """Resolve the three pinned Project versions in one database round trip."""

    row = connection.execute(
        text(
            """
            WITH active_project AS (
                SELECT (now() AT TIME ZONE timezone_name)::date AS business_date
                FROM auditcore.projects
                WHERE tenant_id = :tenant_id AND project_status = 'ACTIVE'
                LIMIT 1
            )
            SELECT
                (
                    SELECT document_requirement_profile_version_id
                    FROM auditcore.document_requirement_profile_versions v
                    CROSS JOIN active_project p
                    WHERE v.tenant_id = :tenant_id
                      AND v.lifecycle_status = 'PUBLISHED'
                      AND v.effective_from <= p.business_date
                    ORDER BY v.effective_from DESC, v.version_no DESC,
                             v.document_requirement_profile_version_id DESC
                    LIMIT 1
                ) AS document_profile_version_id,
                (
                    SELECT policy_version_id
                    FROM auditcore.project_policy_versions v
                    CROSS JOIN active_project p
                    WHERE v.tenant_id = :tenant_id
                      AND v.lifecycle_status = 'PUBLISHED'
                      AND v.effective_from <= p.business_date
                    ORDER BY v.effective_from DESC, v.version_no DESC,
                             v.policy_version_id DESC
                    LIMIT 1
                ) AS policy_version_id,
                (
                    SELECT price_list_version_id
                    FROM auditcore.price_list_versions v
                    CROSS JOIN active_project p
                    WHERE v.tenant_id = :tenant_id
                      AND v.lifecycle_status = 'PUBLISHED'
                      AND v.effective_from <= p.business_date
                      AND (v.effective_to IS NULL OR v.effective_to >= p.business_date)
                    ORDER BY v.effective_from DESC, v.version_no DESC,
                             v.price_list_version_id DESC
                    LIMIT 1
                ) AS price_list_version_id
            FROM active_project
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Project is not active",
            detail="Create Booking is available only for an active Project.",
        )
    return dict(row)


def _create_context(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    outlet_id: UUID,
) -> dict[str, Any]:
    """Resolve the selected PC Outlet and all effective Project versions together."""

    row = connection.execute(
        text(
            """
            WITH selected_outlet AS MATERIALIZED (
                SELECT
                    o.outlet_id,
                    o.outlet_name,
                    o.outlet_classification,
                    d.dealer_id,
                    d.dealer_name
                FROM auditcore.dealer_outlets o
                JOIN auditcore.dealers d
                  ON d.tenant_id = o.tenant_id
                 AND d.dealer_id = o.dealer_id
                WHERE o.tenant_id = :tenant_id
                  AND o.outlet_id = :outlet_id
                  AND o.status = 'ACTIVE'
                  AND d.status = 'ACTIVE'
                  AND EXISTS (
                      SELECT 1
                      FROM auditcore.business_assignments ba
                      WHERE ba.tenant_id = o.tenant_id
                        AND ba.security_actor_id = :actor_id
                        AND ba.business_role_code = 'PC'
                        AND ba.assignment_status = 'ACTIVE'
                        AND ba.effective_from <= now()
                        AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                        AND ba.dealer_id = o.dealer_id
                        AND ba.outlet_id = o.outlet_id
                  )
                LIMIT 1
            ),
            active_project AS MATERIALIZED (
                SELECT (now() AT TIME ZONE timezone_name)::date AS business_date
                FROM auditcore.projects
                WHERE tenant_id = :tenant_id AND project_status = 'ACTIVE'
                LIMIT 1
            )
            SELECT
                so.outlet_id,
                so.outlet_name,
                so.outlet_classification,
                so.dealer_id,
                so.dealer_name,
                (
                    SELECT document_requirement_profile_version_id
                    FROM auditcore.document_requirement_profile_versions v
                    CROSS JOIN active_project p
                    WHERE v.tenant_id = :tenant_id
                      AND v.lifecycle_status = 'PUBLISHED'
                      AND v.effective_from <= p.business_date
                    ORDER BY v.effective_from DESC, v.version_no DESC,
                             v.document_requirement_profile_version_id DESC
                    LIMIT 1
                ) AS document_profile_version_id,
                (
                    SELECT policy_version_id
                    FROM auditcore.project_policy_versions v
                    CROSS JOIN active_project p
                    WHERE v.tenant_id = :tenant_id
                      AND v.lifecycle_status = 'PUBLISHED'
                      AND v.effective_from <= p.business_date
                    ORDER BY v.effective_from DESC, v.version_no DESC,
                             v.policy_version_id DESC
                    LIMIT 1
                ) AS policy_version_id,
                (
                    SELECT price_list_version_id
                    FROM auditcore.price_list_versions v
                    CROSS JOIN active_project p
                    WHERE v.tenant_id = :tenant_id
                      AND v.lifecycle_status = 'PUBLISHED'
                      AND v.effective_from <= p.business_date
                      AND (v.effective_to IS NULL OR v.effective_to >= p.business_date)
                    ORDER BY v.effective_from DESC, v.version_no DESC,
                             v.price_list_version_id DESC
                    LIMIT 1
                ) AS price_list_version_id,
                EXISTS (SELECT 1 FROM active_project) AS project_active
            FROM selected_outlet so
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "outlet_id": outlet_id,
        },
    ).mappings().one_or_none()
    if row is None:
        # Preserve the existing authorization boundary: an invalid/unassigned Outlet
        # remains indistinguishable from an inaccessible Outlet.
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )
    if not bool(row["project_active"]):
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Project is not active",
            detail="Create Booking is available only for an active Project.",
        )
    return dict(row)


def _execute_create_booking_atomic(
    connection: Connection,
    *,
    tenant_id: str,
    context: dict[str, Any],
    customer_name: str,
    actor_id: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    """Create/replay the Booking in one PostgreSQL round trip.

    The transaction advisory lock, replay check, aggregate inserts, workflow event,
    and replay record are one statement. This preserves the same idempotency and FK
    transaction semantics while removing the generic three-round-trip command path.
    """

    request_hash = stable_request_hash(request_payload)
    lock_key = f"{tenant_id}:{_OPERATION_KEY}:{idempotency_key}"
    safe_payload = json.dumps(
        {
            "outletId": str(context["outlet_id"]),
            "customerNameCaptured": True,
        }
    )

    row = connection.execute(
        text(
            """
            WITH lock_guard AS MATERIALIZED (
                SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))
            ),
            existing AS MATERIALIZED (
                SELECT r.request_hash, r.response_body
                FROM lock_guard
                JOIN auditcore.idempotency_records r
                  ON r.tenant_id = :tenant_id
                 AND r.operation_key = :operation_key
                 AND r.idempotency_key = :idempotency_key
            ),
            new_customer AS (
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code,
                    display_name, created_by_actor_id
                )
                SELECT
                    :tenant_id, :dealer_id, :outlet_id, 'PENDING',
                    :customer_name, :actor_id
                FROM lock_guard
                WHERE NOT EXISTS (SELECT 1 FROM existing)
                RETURNING customer_id
            ),
            new_journey AS (
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id,
                    document_requirement_profile_version_id,
                    policy_version_id, price_list_version_id,
                    created_by_actor_id
                )
                SELECT
                    :tenant_id, :dealer_id, :outlet_id, c.customer_id,
                    :document_profile_version_id,
                    :policy_version_id, :price_list_version_id,
                    :actor_id
                FROM new_customer c
                RETURNING journey_id, customer_id
            ),
            new_stage AS (
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status,
                    audit_state, audit_status, first_started_at_utc,
                    latest_activity_at_utc, version_no
                )
                SELECT
                    :tenant_id, j.journey_id, 'BOOKING', 'BOOKING_STARTED',
                    'NOT_STARTED', 'NOT_EVALUATED', now(), now(), 1
                FROM new_journey j
                RETURNING journey_id, business_status, version_no
            ),
            creation_event AS (
                INSERT INTO auditcore.journey_workflow_events (
                    tenant_id, journey_id, stage_code, event_type, source_kind,
                    actor_id, actor_role_snapshot, idempotency_key, correlation_id,
                    safe_payload, occurred_at_utc, aggregate_version
                )
                SELECT
                    :tenant_id, s.journey_id, 'BOOKING', 'BOOKING_CREATED', 'HUMAN',
                    :actor_id, 'PC', :idempotency_key, :idempotency_key,
                    CAST(:safe_payload AS jsonb), now(), s.version_no
                FROM new_stage s
                RETURNING event_id, journey_id
            ),
            new_response AS MATERIALIZED (
                SELECT jsonb_build_object(
                    'journeyId', j.journey_id::text,
                    'customerId', j.customer_id::text,
                    'dealerId', CAST(:dealer_id AS text),
                    'outletId', CAST(:outlet_id AS text),
                    'businessStatus', s.business_status,
                    'aggregateVersion', s.version_no
                ) AS response_body
                FROM new_journey j
                JOIN new_stage s ON s.journey_id = j.journey_id
                JOIN creation_event e ON e.journey_id = j.journey_id
            ),
            recorded AS (
                INSERT INTO auditcore.idempotency_records (
                    tenant_id, operation_key, idempotency_key, request_hash,
                    response_status, response_body
                )
                SELECT
                    :tenant_id, :operation_key, :idempotency_key, :request_hash,
                    201, nr.response_body
                FROM new_response nr
                RETURNING 1
            )
            SELECT
                true AS replayed,
                e.request_hash AS stored_request_hash,
                e.response_body
            FROM existing e
            UNION ALL
            SELECT
                false AS replayed,
                :request_hash AS stored_request_hash,
                nr.response_body
            FROM new_response nr
            CROSS JOIN recorded
            LIMIT 1
            """
        ),
        {
            "lock_key": lock_key,
            "tenant_id": tenant_id,
            "operation_key": _OPERATION_KEY,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "dealer_id": context["dealer_id"],
            "outlet_id": context["outlet_id"],
            "customer_name": customer_name,
            "actor_id": actor_id,
            "document_profile_version_id": context["document_profile_version_id"],
            "policy_version_id": context["policy_version_id"],
            "price_list_version_id": context["price_list_version_id"],
            "safe_payload": safe_payload,
        },
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("Create Booking did not produce an idempotent result")

    if bool(row["replayed"]):
        if row["stored_request_hash"] != request_hash:
            raise ConflictError(
                error_code="VAC-CONFLICT-003",
                title="Idempotency conflict",
                detail="The Idempotency-Key was already used with a different request.",
            )
        if row["response_body"] is None:
            raise ConflictError(
                error_code="VAC-CONFLICT-003",
                title="Idempotency conflict",
                detail="The prior command has no replayable response.",
            )

    body = row["response_body"]
    if not isinstance(body, dict):
        raise RuntimeError("Create Booking idempotent response has invalid shape")
    return dict(body)


@router.post(
    "/bookings",
    response_model=CreateBookingResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_booking(
    tenant_id: str,
    payload: CreateBookingCommand,
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
) -> CreateBookingResponse:
    _authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    context = _create_context(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        outlet_id=payload.outletId,
    )

    customer_name = " ".join(payload.customerName.split())
    if not customer_name:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail="Customer Name is required before Booking details can be captured.",
        )

    request_payload = payload.model_dump(mode="json")
    body = _execute_create_booking_atomic(
        connection,
        tenant_id=tenant_id,
        context=context,
        customer_name=customer_name,
        actor_id=human_principal.subject,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
    )
    return CreateBookingResponse.model_validate(body)
