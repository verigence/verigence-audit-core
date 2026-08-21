from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, text

from audit_core.errors import ConflictError
from audit_core.idempotency import stable_request_hash

_RUNTIME_ROLE = "audit_core_runtime"


@dataclass(frozen=True)
class AdministrativeOperation:
    operation_id: str
    operation_type: str
    tenant_id: str | None
    idempotency_key: str
    status: str
    current_step: str | None
    semantic_request_hash: str
    safe_request_summary: dict[str, Any] | None
    security_receipt: dict[str, Any] | None
    audit_core_receipt: dict[str, Any] | None
    di_receipt: dict[str, Any] | None
    last_error_code: str | None
    last_error_summary: str | None


@contextmanager
def administrative_operation_lock(
    engine: Engine,
    *,
    operation_type: str,
    tenant_id: str | None,
    idempotency_key: str,
) -> Iterator[None]:
    scope = tenant_id if tenant_id is not None else "<platform>"
    lock_key = f"{operation_type}:{scope}:{idempotency_key}"
    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )


def claim_administrative_operation(
    engine: Engine,
    *,
    operation_type: str,
    tenant_id: str | None,
    idempotency_key: str,
    semantic_payload: Any,
    safe_request_summary: dict[str, Any],
    initiated_by_user_id: str,
    correlation_id: str | None,
) -> AdministrativeOperation:
    request_hash = stable_request_hash(semantic_payload)
    with engine.begin() as connection:
        connection.execute(text(f"SET LOCAL ROLE {_RUNTIME_ROLE}"))
        if tenant_id is None:
            existing = connection.execute(
                text(
                    """
                    SELECT operation_id, operation_type, tenant_id, idempotency_key,
                           status, current_step, semantic_request_hash, safe_request_summary,
                           security_receipt, audit_core_receipt, di_receipt,
                           last_error_code, last_error_summary
                    FROM auditcore.administrative_operations
                    WHERE operation_type = :operation_type
                      AND idempotency_key = :idempotency_key
                    ORDER BY created_at_utc
                    LIMIT 1
                    """
                ),
                {
                    "operation_type": operation_type,
                    "idempotency_key": idempotency_key,
                },
            ).mappings().one_or_none()
        else:
            existing = connection.execute(
                text(
                    """
                    SELECT operation_id, operation_type, tenant_id, idempotency_key,
                           status, current_step, semantic_request_hash, safe_request_summary,
                           security_receipt, audit_core_receipt, di_receipt,
                           last_error_code, last_error_summary
                    FROM auditcore.administrative_operations
                    WHERE operation_type = :operation_type
                      AND tenant_id IS NOT DISTINCT FROM :tenant_id
                      AND idempotency_key = :idempotency_key
                    """
                ),
                {
                    "operation_type": operation_type,
                    "tenant_id": tenant_id,
                    "idempotency_key": idempotency_key,
                },
            ).mappings().one_or_none()
        if existing is not None:
            if existing["semantic_request_hash"] != request_hash:
                raise ConflictError(
                    error_code="VAC-CONFLICT-003",
                    title="Idempotency conflict",
                    detail="The Idempotency-Key was already used with a different request.",
                )
            return _operation(existing)

        operation_id = str(uuid4())
        row = connection.execute(
            text(
                """
                INSERT INTO auditcore.administrative_operations (
                    operation_id, operation_type, tenant_id, idempotency_key,
                    semantic_request_hash, status, current_step, initiated_by_user_id,
                    correlation_id, safe_request_summary
                ) VALUES (
                    :operation_id, :operation_type, :tenant_id, :idempotency_key,
                    :request_hash, 'RECEIVED', NULL, :initiated_by_user_id,
                    :correlation_id, CAST(:safe_request_summary AS jsonb)
                )
                RETURNING operation_id, operation_type, tenant_id, idempotency_key,
                          status, current_step, semantic_request_hash, safe_request_summary,
                          security_receipt, audit_core_receipt, di_receipt,
                          last_error_code, last_error_summary
                """
            ),
            {
                "operation_id": operation_id,
                "operation_type": operation_type,
                "tenant_id": tenant_id,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "initiated_by_user_id": initiated_by_user_id,
                "correlation_id": correlation_id,
                "safe_request_summary": json.dumps(safe_request_summary, default=str),
            },
        ).mappings().one()
        return _operation(row)


def get_administrative_operation(
    engine: Engine,
    *,
    operation_id: str,
    operation_type: str | None = None,
) -> AdministrativeOperation | None:
    with engine.connect() as connection:
        connection.execute(text(f"SET ROLE {_RUNTIME_ROLE}"))
        filters = ["operation_id = :operation_id"]
        parameters: dict[str, object] = {"operation_id": operation_id}
        if operation_type is not None:
            filters.append("operation_type = :operation_type")
            parameters["operation_type"] = operation_type
        row = connection.execute(
            text(
                f"""
                SELECT operation_id, operation_type, tenant_id, idempotency_key,
                       status, current_step, semantic_request_hash, safe_request_summary,
                       security_receipt, audit_core_receipt, di_receipt,
                       last_error_code, last_error_summary
                FROM auditcore.administrative_operations
                WHERE {' AND '.join(filters)}
                """
            ),
            parameters,
        ).mappings().one_or_none()
    return _operation(row) if row is not None else None


def update_administrative_operation(
    engine: Engine,
    *,
    operation_id: str,
    status: str,
    current_step: str | None,
    tenant_id: str | None = None,
    security_receipt: dict[str, Any] | None = None,
    audit_core_receipt: dict[str, Any] | None = None,
    di_receipt: dict[str, Any] | None = None,
    last_error_code: str | None = None,
    last_error_summary: str | None = None,
    completed: bool = False,
) -> None:
    with engine.begin() as connection:
        connection.execute(text(f"SET LOCAL ROLE {_RUNTIME_ROLE}"))
        connection.execute(
            text(
                """
                UPDATE auditcore.administrative_operations
                SET tenant_id = COALESCE(:tenant_id, tenant_id),
                    status = :status,
                    current_step = :current_step,
                    security_receipt = COALESCE(
                        CAST(:security_receipt AS jsonb), security_receipt
                    ),
                    audit_core_receipt = COALESCE(
                        CAST(:audit_core_receipt AS jsonb), audit_core_receipt
                    ),
                    di_receipt = COALESCE(
                        CAST(:di_receipt AS jsonb), di_receipt
                    ),
                    last_error_code = :last_error_code,
                    last_error_summary = :last_error_summary,
                    updated_at_utc = now(),
                    completed_at_utc = CASE
                        WHEN :completed THEN now()
                        ELSE completed_at_utc
                    END
                WHERE operation_id = :operation_id
                """
            ),
            {
                "operation_id": operation_id,
                "tenant_id": tenant_id,
                "status": status,
                "current_step": current_step,
                "security_receipt": (
                    json.dumps(security_receipt, default=str)
                    if security_receipt is not None
                    else None
                ),
                "audit_core_receipt": (
                    json.dumps(audit_core_receipt, default=str)
                    if audit_core_receipt is not None
                    else None
                ),
                "di_receipt": json.dumps(di_receipt, default=str) if di_receipt is not None else None,
                "last_error_code": last_error_code,
                "last_error_summary": last_error_summary,
                "completed": completed,
            },
        )


def _operation(row: Any) -> AdministrativeOperation:
    return AdministrativeOperation(
        operation_id=str(row["operation_id"]),
        operation_type=str(row["operation_type"]),
        tenant_id=(str(row["tenant_id"]) if row["tenant_id"] is not None else None),
        idempotency_key=str(row["idempotency_key"]),
        status=str(row["status"]),
        current_step=(str(row["current_step"]) if row["current_step"] is not None else None),
        semantic_request_hash=str(row["semantic_request_hash"]),
        safe_request_summary=(
            dict(row["safe_request_summary"])
            if row["safe_request_summary"] is not None
            else None
        ),
        security_receipt=(
            dict(row["security_receipt"])
            if row["security_receipt"] is not None
            else None
        ),
        audit_core_receipt=(
            dict(row["audit_core_receipt"])
            if row["audit_core_receipt"] is not None
            else None
        ),
        di_receipt=(dict(row["di_receipt"]) if row["di_receipt"] is not None else None),
        last_error_code=(
            str(row["last_error_code"]) if row["last_error_code"] is not None else None
        ),
        last_error_summary=(
            str(row["last_error_summary"])
            if row["last_error_summary"] is not None
            else None
        ),
    )