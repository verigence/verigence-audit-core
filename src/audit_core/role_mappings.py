from __future__ import annotations

import os
from collections import defaultdict
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from audit_core.admin_operations import (
    AdministrativeOperation,
    administrative_operation_lock,
    claim_administrative_operation,
    update_administrative_operation,
)
from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_engine,
    require_project_admin_request,
)
from audit_core.errors import AuditCoreError, BusinessValidationError, NotFoundError
from audit_core.observability import get_correlation_id
from audit_core.security_integration import SecurityAdminClient, SecurityAdminError

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["role-mapping"])

OperatingRole = Literal["PC", "TL", "PM", "CRM", "Executive"]
_OPERATION_TYPE = "ROLE_MAPPING"
_RUNTIME_ROLE = "audit_core_runtime"


class RoleMappingCandidateResponse(BaseModel):
    userId: str
    displayName: str
    primaryEmail: str | None
    status: str


class RoleMappingPutRequest(BaseModel):
    operatingRole: OperatingRole
    dealerIds: list[UUID] = Field(default_factory=list)
    outletIds: list[UUID] = Field(default_factory=list)


class RoleMappingResponse(BaseModel):
    userId: str
    operatingRole: OperatingRole
    dealerIds: list[UUID]
    outletIds: list[UUID]


class RoleMappingMutationResponse(BaseModel):
    operationId: UUID
    operationStatus: Literal["COMPLETED"]
    mapping: RoleMappingResponse | None


def _security_base_url() -> str:
    value = os.environ.get("SECURITY_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("SECURITY_BASE_URL is required for UC02 administration")
    return value


def _require_project(connection: Connection, tenant_id: str) -> None:
    exists = connection.execute(
        text("SELECT 1 FROM auditcore.projects WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    if exists is None:
        raise NotFoundError(
            error_code="VAC-NF-001",
            title="Project not found",
            detail="Project not found for the requested tenant.",
        )


def _dependency_unavailable(
    detail: str = "Project administration is temporarily unavailable.",
) -> AuditCoreError:
    return AuditCoreError(
        error_code="VAC-SYS-002",
        status_code=503,
        title="Dependency unavailable",
        detail=detail,
    )


def _runtime_transaction(engine: Engine):
    return engine.begin()


def _active_assignment_rows(
    connection: Connection,
    *,
    tenant_id: str,
    user_id: str | None = None,
) -> list[Any]:
    clauses = [
        "tenant_id = :tenant_id",
        "assignment_status = 'ACTIVE'",
        "effective_from <= now()",
        "(effective_to IS NULL OR effective_to > now())",
    ]
    params: dict[str, object] = {"tenant_id": tenant_id}
    if user_id is not None:
        clauses.append("security_actor_id = :user_id")
        params["user_id"] = user_id
    return list(
        connection.execute(
            text(
                "SELECT assignment_id, security_actor_id, business_role_code, "
                "dealer_id, outlet_id FROM auditcore.business_assignments WHERE "
                + " AND ".join(clauses)
                + " ORDER BY security_actor_id, assignment_id"
            ),
            params,
        ).mappings()
    )


def _mapping_from_rows(user_id: str, rows: list[Any]) -> RoleMappingResponse:
    roles = {str(row["business_role_code"]) for row in rows}
    if len(roles) != 1:
        raise RuntimeError("Active business assignments have inconsistent operating roles")
    role = next(iter(roles))
    if role not in {"PC", "TL", "PM", "CRM", "Executive"}:
        raise RuntimeError("Active business assignment has unsupported operating role")

    dealer_ids = sorted(
        {UUID(str(row["dealer_id"])) for row in rows if row["dealer_id"] is not None},
        key=str,
    )
    outlet_ids = sorted(
        {UUID(str(row["outlet_id"])) for row in rows if row["outlet_id"] is not None},
        key=str,
    )

    if role == "PC":
        if not outlet_ids or any(row["dealer_id"] is None for row in rows):
            raise RuntimeError("PC business assignment scope is inconsistent")
        response_dealers: list[UUID] = []
        response_outlets = outlet_ids
    elif role == "TL":
        if not dealer_ids or any(row["outlet_id"] is not None for row in rows):
            raise RuntimeError("TL business assignment scope is inconsistent")
        response_dealers = dealer_ids
        response_outlets = []
    elif role in {"PM", "Executive"}:
        if dealer_ids or outlet_ids:
            raise RuntimeError("Project-wide business assignment scope is inconsistent")
        response_dealers = []
        response_outlets = []
    else:
        project_wide = all(
            row["dealer_id"] is None and row["outlet_id"] is None for row in rows
        )
        dealer_wide = all(
            row["dealer_id"] is not None and row["outlet_id"] is None for row in rows
        )
        if not project_wide and not dealer_wide:
            raise RuntimeError("CRM business assignment scope is inconsistent")
        response_dealers = [] if project_wide else dealer_ids
        response_outlets = []

    return RoleMappingResponse(
        userId=user_id,
        operatingRole=role,  # type: ignore[arg-type]
        dealerIds=response_dealers,
        outletIds=response_outlets,
    )


def _list_mappings(connection: Connection, tenant_id: str) -> list[RoleMappingResponse]:
    rows = _active_assignment_rows(connection, tenant_id=tenant_id)
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[str(row["security_actor_id"])].append(row)
    return [
        _mapping_from_rows(user_id, grouped[user_id])
        for user_id in sorted(grouped)
    ]


def _get_mapping(
    connection: Connection,
    tenant_id: str,
    user_id: str,
) -> RoleMappingResponse | None:
    rows = _active_assignment_rows(connection, tenant_id=tenant_id, user_id=user_id)
    if not rows:
        return None
    return _mapping_from_rows(user_id, rows)


def _read_mapping(
    engine: Engine,
    tenant_id: str,
    user_id: str,
) -> RoleMappingResponse | None:
    with engine.begin() as connection:
        connection.execute(text(f"SET LOCAL ROLE {_RUNTIME_ROLE}"))
        set_tenant_context(connection, tenant_id)
        _require_project(connection, tenant_id)
        return _get_mapping(connection, tenant_id, user_id)


def _resolve_scope(
    engine: Engine,
    *,
    tenant_id: str,
    body: RoleMappingPutRequest,
) -> tuple[list[UUID], list[UUID], list[tuple[UUID | None, UUID | None]]]:
    dealer_ids = sorted(set(body.dealerIds), key=str)
    outlet_ids = sorted(set(body.outletIds), key=str)

    if body.operatingRole == "PC":
        if dealer_ids or not outlet_ids:
            raise BusinessValidationError(
                detail="PC Role Mapping requires one or more Outlets and no Dealer IDs."
            )
    elif body.operatingRole == "TL":
        if not dealer_ids or outlet_ids:
            raise BusinessValidationError(
                detail="TL Role Mapping requires one or more Dealers and no Outlet IDs."
            )
    elif body.operatingRole in {"PM", "Executive"}:
        if dealer_ids or outlet_ids:
            raise BusinessValidationError(
                detail=f"{body.operatingRole} Role Mapping is Project-wide."
            )
    elif outlet_ids:
        raise BusinessValidationError(detail="CRM Role Mapping does not accept Outlet IDs.")

    with engine.begin() as connection:
        connection.execute(text(f"SET LOCAL ROLE {_RUNTIME_ROLE}"))
        set_tenant_context(connection, tenant_id)
        _require_project(connection, tenant_id)

        if body.operatingRole == "PC":
            scopes: list[tuple[UUID | None, UUID | None]] = []
            for outlet_id in outlet_ids:
                dealer_id = connection.execute(
                    text(
                        "SELECT dealer_id FROM auditcore.dealer_outlets "
                        "WHERE tenant_id=:tenant_id AND outlet_id=:outlet_id"
                    ),
                    {"tenant_id": tenant_id, "outlet_id": outlet_id},
                ).scalar_one_or_none()
                if dealer_id is None:
                    raise BusinessValidationError(
                        detail="Each PC Outlet must belong to the requested Project."
                    )
                scopes.append((UUID(str(dealer_id)), outlet_id))
            return [], outlet_ids, scopes

        if body.operatingRole in {"TL", "CRM"} and dealer_ids:
            for dealer_id in dealer_ids:
                exists = connection.execute(
                    text(
                        "SELECT 1 FROM auditcore.dealers "
                        "WHERE tenant_id=:tenant_id AND dealer_id=:dealer_id"
                    ),
                    {"tenant_id": tenant_id, "dealer_id": dealer_id},
                ).scalar_one_or_none()
                if exists is None:
                    raise BusinessValidationError(
                        detail="Each Dealer scope must belong to the requested Project."
                    )
            return dealer_ids, [], [(dealer_id, None) for dealer_id in dealer_ids]

        return [], [], [(None, None)]


def _desired_signature(
    role: str,
    scopes: list[tuple[UUID | None, UUID | None]],
) -> list[tuple[str, str | None, str | None]]:
    return sorted(
        [
            (
                role,
                str(dealer_id) if dealer_id is not None else None,
                str(outlet_id) if outlet_id is not None else None,
            )
            for dealer_id, outlet_id in scopes
        ],
        key=str,
    )


def _replace_assignments(
    engine: Engine,
    *,
    tenant_id: str,
    user_id: str,
    role: str,
    scopes: list[tuple[UUID | None, UUID | None]],
    actor_user_id: str,
) -> bool:
    with engine.begin() as connection:
        connection.execute(text(f"SET LOCAL ROLE {_RUNTIME_ROLE}"))
        set_tenant_context(connection, tenant_id)
        _require_project(connection, tenant_id)
        current = _active_assignment_rows(connection, tenant_id=tenant_id, user_id=user_id)
        current_signature = sorted(
            [
                (
                    str(row["business_role_code"]),
                    str(row["dealer_id"]) if row["dealer_id"] is not None else None,
                    str(row["outlet_id"]) if row["outlet_id"] is not None else None,
                )
                for row in current
            ],
            key=str,
        )
        if current_signature == _desired_signature(role, scopes):
            return False

        connection.execute(
            text(
                """
                UPDATE auditcore.business_assignments
                SET assignment_status='INACTIVE', effective_to=now(), updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND security_actor_id=:user_id
                  AND assignment_status='ACTIVE'
                  AND (effective_to IS NULL OR effective_to > now())
                """
            ),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        for dealer_id, outlet_id in scopes:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.business_assignments (
                        tenant_id, security_actor_id, business_role_code,
                        dealer_id, outlet_id, created_by_actor_id
                    ) VALUES (
                        :tenant_id, :user_id, :role, :dealer_id, :outlet_id, :actor_user_id
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "role": role,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                    "actor_user_id": actor_user_id,
                },
            )
        return True


def _deactivate_assignments(engine: Engine, *, tenant_id: str, user_id: str) -> int:
    with engine.begin() as connection:
        connection.execute(text(f"SET LOCAL ROLE {_RUNTIME_ROLE}"))
        set_tenant_context(connection, tenant_id)
        _require_project(connection, tenant_id)
        result = connection.execute(
            text(
                """
                UPDATE auditcore.business_assignments
                SET assignment_status='INACTIVE', effective_to=now(), updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND security_actor_id=:user_id
                  AND assignment_status='ACTIVE'
                  AND (effective_to IS NULL OR effective_to > now())
                """
            ),
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        return int(result.rowcount or 0)


def _raise_security_error(exc: SecurityAdminError) -> None:
    if exc.http_status == 401:
        raise AuditCoreError(
            error_code="VAC-AUTH-001",
            status_code=401,
            title="Authentication required",
            detail="Authentication is required for this administrative operation.",
        ) from exc
    if exc.http_status == 403:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        ) from exc
    if exc.http_status is not None and exc.http_status < 500:
        raise BusinessValidationError(
            detail="The requested role mapping could not be applied."
        ) from exc
    raise _dependency_unavailable(
        "Role mapping could not be completed. Please try again."
    ) from exc


def _restore_security_role(
    *,
    admin_request: HumanAdminRequest,
    tenant_id: str,
    user_id: str,
    previous: RoleMappingResponse | None,
) -> None:
    with SecurityAdminClient(base_url=_security_base_url()) as client:
        if previous is None:
            result = client.remove_operating_role(
                human_bearer_token=admin_request.bearer_token,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            if result.tenant_id != tenant_id or result.user_id != user_id:
                raise RuntimeError(
                    "Security compensation receipt does not match role removal"
                )
        else:
            result = client.set_operating_role(
                human_bearer_token=admin_request.bearer_token,
                tenant_id=tenant_id,
                user_id=user_id,
                role_key=previous.operatingRole,
            )
            if (
                result.tenant_id != tenant_id
                or result.user_id != user_id
                or result.role_key != previous.operatingRole
            ):
                raise RuntimeError(
                    "Security compensation receipt does not match prior role"
                )


def _operation_response(
    operation: AdministrativeOperation,
    *,
    mapping: RoleMappingResponse | None,
) -> RoleMappingMutationResponse:
    return RoleMappingMutationResponse(
        operationId=UUID(operation.operation_id),
        operationStatus="COMPLETED",
        mapping=mapping,
    )


def _mark_operation_failed(
    engine: Engine,
    *,
    operation: AdministrativeOperation,
    current_step: str,
    summary: str,
) -> None:
    try:
        update_administrative_operation(
            engine,
            operation_id=operation.operation_id,
            status="FAILED",
            current_step=current_step,
            last_error_summary=summary,
        )
    except Exception as exc:
        logger.error(
            "role_mapping_failure_receipt_write_failed",
            operation_id=operation.operation_id,
            exc_type=type(exc).__name__,
        )


@router.get("/role-mapping-candidates", response_model=list[RoleMappingCandidateResponse])
def list_role_mapping_candidates(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    q: str | None = Query(default=None, max_length=320),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[RoleMappingCandidateResponse]:
    set_tenant_context(connection, tenant_id)
    _require_project(connection, tenant_id)
    try:
        with SecurityAdminClient(base_url=_security_base_url()) as client:
            users = client.list_global_users(
                human_bearer_token=admin_request.bearer_token,
                search=q,
                limit=limit,
            )
    except SecurityAdminError as exc:
        raise _dependency_unavailable() from exc
    return [
        RoleMappingCandidateResponse(
            userId=user.user_id,
            displayName=user.display_name,
            primaryEmail=user.primary_email,
            status=user.status,
        )
        for user in users
    ]


@router.get("/role-mappings", response_model=list[RoleMappingResponse])
def list_role_mappings(
    tenant_id: str,
    _: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[RoleMappingResponse]:
    set_tenant_context(connection, tenant_id)
    _require_project(connection, tenant_id)
    return _list_mappings(connection, tenant_id)


@router.get("/role-mappings/{user_id}", response_model=RoleMappingResponse | None)
def get_role_mapping(
    tenant_id: str,
    user_id: str,
    _: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> RoleMappingResponse | None:
    set_tenant_context(connection, tenant_id)
    _require_project(connection, tenant_id)
    return _get_mapping(connection, tenant_id, user_id)


@router.put("/role-mappings/{user_id}", response_model=RoleMappingMutationResponse)
def put_role_mapping(
    tenant_id: str,
    user_id: str,
    body: RoleMappingPutRequest,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> RoleMappingMutationResponse:
    dealer_ids, outlet_ids, scopes = _resolve_scope(
        engine,
        tenant_id=tenant_id,
        body=body,
    )
    mapping = RoleMappingResponse(
        userId=user_id,
        operatingRole=body.operatingRole,
        dealerIds=dealer_ids,
        outletIds=outlet_ids,
    )
    semantic_payload = {
        "userId": user_id,
        "operatingRole": body.operatingRole,
        "dealerIds": [str(value) for value in dealer_ids],
        "outletIds": [str(value) for value in outlet_ids],
    }

    with administrative_operation_lock(
        engine,
        operation_type=_OPERATION_TYPE,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
    ):
        operation = claim_administrative_operation(
            engine,
            operation_type=_OPERATION_TYPE,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            semantic_payload=semantic_payload,
            safe_request_summary=semantic_payload,
            initiated_by_user_id=admin_request.user_id,
            correlation_id=get_correlation_id(request),
        )
        if operation.status == "COMPLETED":
            return _operation_response(operation, mapping=mapping)

        previous = _read_mapping(engine, tenant_id, user_id)
        try:
            with SecurityAdminClient(base_url=_security_base_url()) as client:
                result = client.set_operating_role(
                    human_bearer_token=admin_request.bearer_token,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    role_key=body.operatingRole,
                )
        except SecurityAdminError as exc:
            _mark_operation_failed(
                engine,
                operation=operation,
                current_step="SECURITY",
                summary="Operating-role update failed before local business-scope change.",
            )
            _raise_security_error(exc)

        if (
            result.tenant_id != tenant_id
            or result.user_id != user_id
            or result.role_key != body.operatingRole
        ):
            try:
                _restore_security_role(
                    admin_request=admin_request,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    previous=previous,
                )
            except Exception as compensation_exc:
                _mark_operation_failed(
                    engine,
                    operation=operation,
                    current_step="COMPENSATION",
                    summary="Operating-role compensation failed.",
                )
                logger.critical(
                    "role_mapping_compensation_failed",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    exc_type=type(compensation_exc).__name__,
                )
                raise _dependency_unavailable(
                    "Role mapping could not be completed cleanly. Please contact support."
                ) from compensation_exc
            _mark_operation_failed(
                engine,
                operation=operation,
                current_step="SECURITY",
                summary="Operating-role receipt did not match the requested mapping.",
            )
            raise _dependency_unavailable(
                "Role mapping could not be completed. Please try again."
            )

        try:
            local_changed = _replace_assignments(
                engine,
                tenant_id=tenant_id,
                user_id=user_id,
                role=body.operatingRole,
                scopes=scopes,
                actor_user_id=admin_request.user_id,
            )
        except Exception as exc:
            logger.error(
                "role_mapping_local_write_failed",
                tenant_id=tenant_id,
                user_id=user_id,
                exc_type=type(exc).__name__,
            )
            try:
                _restore_security_role(
                    admin_request=admin_request,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    previous=previous,
                )
            except Exception as compensation_exc:
                _mark_operation_failed(
                    engine,
                    operation=operation,
                    current_step="COMPENSATION",
                    summary="Operating-role compensation failed after local write failure.",
                )
                logger.critical(
                    "role_mapping_compensation_failed",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    exc_type=type(compensation_exc).__name__,
                )
                raise _dependency_unavailable(
                    "Role mapping could not be completed cleanly. Please contact support."
                ) from compensation_exc
            _mark_operation_failed(
                engine,
                operation=operation,
                current_step="AUDIT_CORE",
                summary="Business-scope update failed and was compensated.",
            )
            raise _dependency_unavailable(
                "Role mapping could not be completed. Please try again."
            ) from exc

        update_administrative_operation(
            engine,
            operation_id=operation.operation_id,
            status="COMPLETED",
            current_step="COMPLETE",
            security_receipt={
                "tenantId": result.tenant_id,
                "userId": result.user_id,
                "changed": result.changed,
                "assignmentId": result.assignment_id,
                "roleKey": result.role_key,
            },
            audit_core_receipt={
                "userId": user_id,
                "roleKey": body.operatingRole,
                "scopeCount": len(scopes),
                "changed": local_changed,
            },
            last_error_summary=None,
            completed=True,
        )
        return _operation_response(operation, mapping=mapping)


@router.delete("/role-mappings/{user_id}", response_model=RoleMappingMutationResponse)
def delete_role_mapping(
    tenant_id: str,
    user_id: str,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> RoleMappingMutationResponse:
    semantic_payload = {"userId": user_id, "action": "REMOVE"}

    with administrative_operation_lock(
        engine,
        operation_type=_OPERATION_TYPE,
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
    ):
        operation = claim_administrative_operation(
            engine,
            operation_type=_OPERATION_TYPE,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            semantic_payload=semantic_payload,
            safe_request_summary=semantic_payload,
            initiated_by_user_id=admin_request.user_id,
            correlation_id=get_correlation_id(request),
        )
        if operation.status == "COMPLETED":
            return _operation_response(operation, mapping=None)

        previous = _read_mapping(engine, tenant_id, user_id)
        try:
            with SecurityAdminClient(base_url=_security_base_url()) as client:
                result = client.remove_operating_role(
                    human_bearer_token=admin_request.bearer_token,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
        except SecurityAdminError as exc:
            _mark_operation_failed(
                engine,
                operation=operation,
                current_step="SECURITY",
                summary="Operating-role removal failed before local assignment removal.",
            )
            _raise_security_error(exc)

        if result.tenant_id != tenant_id or result.user_id != user_id:
            if previous is not None:
                try:
                    _restore_security_role(
                        admin_request=admin_request,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        previous=previous,
                    )
                except Exception as compensation_exc:
                    _mark_operation_failed(
                        engine,
                        operation=operation,
                        current_step="COMPENSATION",
                        summary="Operating-role removal compensation failed.",
                    )
                    logger.critical(
                        "role_mapping_remove_compensation_failed",
                        tenant_id=tenant_id,
                        user_id=user_id,
                        exc_type=type(compensation_exc).__name__,
                    )
                    raise _dependency_unavailable(
                        "Role mapping removal could not be completed cleanly. Please contact support."
                    ) from compensation_exc
            _mark_operation_failed(
                engine,
                operation=operation,
                current_step="SECURITY",
                summary="Operating-role removal receipt did not match the request.",
            )
            raise _dependency_unavailable(
                "Role mapping removal could not be completed. Please try again."
            )

        try:
            deactivated = _deactivate_assignments(
                engine,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        except Exception as exc:
            logger.error(
                "role_mapping_local_remove_failed",
                tenant_id=tenant_id,
                user_id=user_id,
                exc_type=type(exc).__name__,
            )
            if previous is not None:
                try:
                    _restore_security_role(
                        admin_request=admin_request,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        previous=previous,
                    )
                except Exception as compensation_exc:
                    _mark_operation_failed(
                        engine,
                        operation=operation,
                        current_step="COMPENSATION",
                        summary=(
                            "Operating-role removal compensation failed after local write failure."
                        ),
                    )
                    logger.critical(
                        "role_mapping_remove_compensation_failed",
                        tenant_id=tenant_id,
                        user_id=user_id,
                        exc_type=type(compensation_exc).__name__,
                    )
                    raise _dependency_unavailable(
                        "Role mapping removal could not be completed cleanly. Please contact support."
                    ) from compensation_exc
            _mark_operation_failed(
                engine,
                operation=operation,
                current_step="AUDIT_CORE",
                summary="Business-assignment removal failed and was compensated.",
            )
            raise _dependency_unavailable(
                "Role mapping removal could not be completed. Please try again."
            ) from exc

        update_administrative_operation(
            engine,
            operation_id=operation.operation_id,
            status="COMPLETED",
            current_step="COMPLETE",
            security_receipt={
                "tenantId": result.tenant_id,
                "userId": result.user_id,
                "changed": result.changed,
                "assignmentId": result.assignment_id,
                "roleKey": result.role_key,
            },
            audit_core_receipt={
                "userId": user_id,
                "deactivatedAssignments": deactivated,
            },
            last_error_summary=None,
            completed=True,
        )
        return _operation_response(operation, mapping=None)
