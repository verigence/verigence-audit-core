from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from audit_core.errors import ConflictError
from audit_core.idempotency import stable_request_hash

_OPERATION_KEY = "uc03.booking.create"


def execute_simplified_create_booking_atomic(
    connection: Connection,
    *,
    tenant_id: str,
    context: dict[str, Any],
    actor_id: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    """Create Customer + Journey atomically with Journey ID as the initial reference.

    The Journey UUID is generated inside the same SQL statement before the Customer row
    is inserted. That UUID is written directly to customers.display_name and then used
    as journeys.journey_id. This avoids any post-Journey Customer name mutation, which
    is forbidden by the entered-name immutability trigger.
    """

    request_hash = stable_request_hash(request_payload)
    lock_key = f"{tenant_id}:{_OPERATION_KEY}:{idempotency_key}"
    safe_payload = json.dumps(
        {
            "outletId": str(context["outlet_id"]),
            "customerNameCaptured": False,
            "referenceMode": "JOURNEY_ID",
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
            new_ids AS MATERIALIZED (
                SELECT gen_random_uuid() AS journey_id
                FROM lock_guard
                WHERE NOT EXISTS (SELECT 1 FROM existing)
            ),
            new_customer AS (
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code,
                    display_name, created_by_actor_id
                )
                SELECT
                    :tenant_id, :dealer_id, :outlet_id, 'PENDING',
                    ids.journey_id::text, :actor_id
                FROM new_ids ids
                RETURNING customer_id
            ),
            new_journey AS (
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id, journey_id,
                    document_requirement_profile_version_id,
                    policy_version_id, price_list_version_id,
                    created_by_actor_id
                )
                SELECT
                    :tenant_id, :dealer_id, :outlet_id, c.customer_id, ids.journey_id,
                    :document_profile_version_id,
                    :policy_version_id, :price_list_version_id,
                    :actor_id
                FROM new_customer c
                CROSS JOIN new_ids ids
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
        raise TypeError("Create Booking idempotent response has invalid shape")
    return dict(body)
