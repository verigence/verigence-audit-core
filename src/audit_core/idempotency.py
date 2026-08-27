from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import Connection, text

from audit_core.errors import ConflictError

T = TypeVar("T")


def stable_request_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execute_idempotent_json_command(
    connection: Connection,
    *,
    tenant_id: str,
    operation_key: str,
    idempotency_key: str,
    request_payload: Any,
    execute: Callable[[], dict[str, Any]],
    response_status: int = 200,
    logical_result_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    request_hash = stable_request_hash(request_payload)
    lock_key = f"{tenant_id}:{operation_key}:{idempotency_key}"

    # Acquire the transaction advisory lock and inspect any replay record in one
    # PostgreSQL round trip. MATERIALIZED guarantees the volatile lock function is
    # evaluated before the lateral idempotency lookup.
    existing = connection.execute(
        text(
            """
            WITH lock_guard AS MATERIALIZED (
                SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))
            )
            SELECT r.request_hash, r.response_body
            FROM lock_guard
            CROSS JOIN LATERAL (
                SELECT request_hash, response_body
                FROM auditcore.idempotency_records
                WHERE tenant_id = :tenant_id
                  AND operation_key = :operation_key
                  AND idempotency_key = :idempotency_key
            ) r
            """
        ),
        {
            "lock_key": lock_key,
            "tenant_id": tenant_id,
            "operation_key": operation_key,
            "idempotency_key": idempotency_key,
        },
    ).mappings().one_or_none()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise ConflictError(
                error_code="VAC-CONFLICT-003",
                title="Idempotency conflict",
                detail="The Idempotency-Key was already used with a different request.",
            )
        if existing["response_body"] is None:
            raise ConflictError(
                error_code="VAC-CONFLICT-003",
                title="Idempotency conflict",
                detail="The prior command has no replayable response.",
            )
        return dict(existing["response_body"]), True

    response = execute()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.idempotency_records (
                tenant_id, operation_key, idempotency_key, request_hash,
                logical_result_id, response_status, response_body
            ) VALUES (
                :tenant_id, :operation_key, :idempotency_key, :request_hash,
                :logical_result_id, :response_status, CAST(:response_body AS jsonb)
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "operation_key": operation_key,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "logical_result_id": logical_result_id,
            "response_status": response_status,
            "response_body": json.dumps(response, default=str),
        },
    )
    return response, False
