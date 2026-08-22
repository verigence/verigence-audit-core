from __future__ import annotations

import os
from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from audit_core.admin_operations import (
    AdministrativeOperation,
    administrative_operation_lock,
    claim_administrative_operation,
    get_administrative_operation,
    update_administrative_operation,
)
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import BusinessValidationError, ConflictError, NotFoundError
from audit_core.security_integration import (
    SecurityAdminClient,
    SecurityAdminError,
    SecurityTenant,
)

router = APIRouter(prefix="/v1", tags=["project-provisioning"])

_OPERATION_TYPE = "PROJECT_PROVISION"


class ProjectCreateRequest(BaseModel):
    projectName: str = Field(min_length=1, max_length=240)
    oemId: UUID
    productCategoryId: UUID
    effectiveStartDate: date
    effectiveEndDate: date | None = None
    timezoneName: str = Field(default="Asia/Kolkata", min_length=1, max_length=100)
    regionCode: str | None = Field(default=None, max_length=100)


class ProjectSelectionResponse(BaseModel):
    tenantId: str
    projectCode: str
    projectName: str
    projectStatus: str
    securityTenantStatus: str


class ProjectProvisioningResponse(BaseModel):
    operationId: UUID
    tenantId: str | None
    projectName: str
    projectStatus: str
    provisioningStatus: Literal["READY", "IN_PROGRESS", "RECOVERY_REQUIRED"]
    currentStep: Literal["SECURITY", "AUDIT_CORE", "DI", "COMPLETE"]
    errorCode: str | None = None
    errorMessage: str | None = None


def _security_base_url() -> str:
    value = os.environ.get("SECURITY_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("SECURITY_BASE_URL is required for UC02 administration")
    return value


def _di_base_url() -> str:
    value = os.environ.get("DI_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("DI_BASE_URL is required for UC02 administration")
    return value


def _semantic_payload(request: ProjectCreateRequest) -> dict[str, Any]:
    return {
        "projectName": request.projectName.strip(),
        "oemId": str(request.oemId),
        "productCategoryId": str(request.productCategoryId),
        "effectiveStartDate": request.effectiveStartDate.isoformat(),
        "effectiveEndDate": (
            request.effectiveEndDate.isoformat() if request.effectiveEndDate is not None else None
        ),
        "timezoneName": request.timezoneName.strip(),
        "regionCode": request.regionCode.strip() if request.regionCode else None,
    }


def _request_from_summary(summary: dict[str, Any]) -> ProjectCreateRequest:
    return ProjectCreateRequest.model_validate(summary)


def _validate_request(engine: Engine, request: ProjectCreateRequest) -> None:
    if (
        request.effectiveEndDate is not None
        and request.effectiveEndDate < request.effectiveStartDate
    ):
        raise BusinessValidationError(
            detail="Effective End Date cannot be earlier than Effective Start Date."
        )
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        oem_exists = connection.execute(
            text("SELECT 1 FROM auditcore.oems WHERE oem_id=:oem_id AND is_active=true"),
            {"oem_id": request.oemId},
        ).scalar_one_or_none()
        if oem_exists is None:
            raise BusinessValidationError(detail="OEM does not reference an active approved value.")
        category_exists = connection.execute(
            text(
                "SELECT 1 FROM auditcore.product_categories "
                "WHERE product_category_id=:category_id AND is_active=true"
            ),
            {"category_id": request.productCategoryId},
        ).scalar_one_or_none()
        if category_exists is None:
            raise BusinessValidationError(
                detail="Product Category does not reference an active approved value."
            )


def _security_receipt(tenant: SecurityTenant) -> dict[str, Any]:
    return {
        "tenantId": tenant.tenant_id,
        "tenantCode": tenant.tenant_code,
        "tenantName": tenant.tenant_name,
        "status": tenant.status,
    }


def _project_selection(
    engine: Engine,
    *,
    tenant: SecurityTenant,
) -> ProjectSelectionResponse | None:
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        set_tenant_context(connection, tenant.tenant_id)
        row = connection.execute(
            text(
                """
                SELECT tenant_id, project_code, project_name, project_status
                FROM auditcore.projects
                WHERE tenant_id=:tenant_id
                """
            ),
            {"tenant_id": tenant.tenant_id},
        ).mappings().one_or_none()
        provisioning_status = connection.execute(
            text(
                """
                SELECT status
                FROM auditcore.administrative_operations
                WHERE operation_type=:operation_type
                  AND tenant_id=:tenant_id
                ORDER BY created_at_utc DESC
                LIMIT 1
                """
            ),
            {"operation_type": _OPERATION_TYPE, "tenant_id": tenant.tenant_id},
        ).scalar_one_or_none()
    if row is None:
        return None
    if provisioning_status is not None and provisioning_status != "COMPLETED":
        return None
    return ProjectSelectionResponse(
        tenantId=str(row["tenant_id"]),
        projectCode=str(row["project_code"]),
        projectName=str(row["project_name"]),
        projectStatus=str(row["project_status"]),
        securityTenantStatus=tenant.status,
    )


def _ensure_project_projection(
    engine: Engine,
    *,
    tenant: SecurityTenant,
    request: ProjectCreateRequest,
    actor_id: str,
) -> dict[str, Any]:
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        set_tenant_context(connection, tenant.tenant_id)
        current = connection.execute(
            text(
                """
                SELECT tenant_id, project_code, project_name, oem_id, product_category_id,
                       effective_start_date, effective_end_date, timezone_name, region_code,
                       project_status
                FROM auditcore.projects
                WHERE tenant_id=:tenant_id
                """
            ),
            {"tenant_id": tenant.tenant_id},
        ).mappings().one_or_none()
        if current is None:
            current = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.projects (
                        tenant_id, project_code, project_name, oem_id, product_category_id,
                        effective_start_date, effective_end_date, timezone_name, region_code,
                        project_status, created_by_actor_id, updated_by_actor_id
                    ) VALUES (
                        :tenant_id, :project_code, :project_name, :oem_id, :category_id,
                        :effective_start, :effective_end, :timezone_name, :region_code,
                        'CONFIGURING', :actor_id, :actor_id
                    )
                    RETURNING tenant_id, project_code, project_name, oem_id, product_category_id,
                              effective_start_date, effective_end_date, timezone_name, region_code,
                              project_status
                    """
                ),
                {
                    "tenant_id": tenant.tenant_id,
                    "project_code": tenant.tenant_code,
                    "project_name": request.projectName.strip(),
                    "oem_id": request.oemId,
                    "category_id": request.productCategoryId,
                    "effective_start": request.effectiveStartDate,
                    "effective_end": request.effectiveEndDate,
                    "timezone_name": request.timezoneName.strip(),
                    "region_code": request.regionCode.strip() if request.regionCode else None,
                    "actor_id": actor_id,
                },
            ).mappings().one()
        expected = {
            "project_name": request.projectName.strip(),
            "oem_id": request.oemId,
            "product_category_id": request.productCategoryId,
            "effective_start_date": request.effectiveStartDate,
            "effective_end_date": request.effectiveEndDate,
            "timezone_name": request.timezoneName.strip(),
            "region_code": request.regionCode.strip() if request.regionCode else None,
        }
        if any(current[key] != value for key, value in expected.items()):
            raise ConflictError(
                error_code="VAC-CONFLICT-003",
                title="Project provisioning conflict",
                detail="The provisioned Security Tenant is already linked to different Project details.",
            )
        return {
            "tenantId": str(current["tenant_id"]),
            "projectCode": str(current["project_code"]),
            "projectStatus": str(current["project_status"]),
        }


def _response(operation: AdministrativeOperation) -> ProjectProvisioningResponse:
    summary = operation.safe_request_summary or {}
    tenant_id = operation.tenant_id
    if tenant_id is None and operation.security_receipt is not None:
        value = operation.security_receipt.get("tenantId")
        tenant_id = str(value) if value else None
    status: Literal["READY", "IN_PROGRESS", "RECOVERY_REQUIRED"]
    if operation.status == "COMPLETED":
        status = "READY"
    elif operation.status in {"RECEIVED", "RUNNING"}:
        status = "IN_PROGRESS"
    else:
        status = "RECOVERY_REQUIRED"
    raw_step = operation.current_step or "SECURITY"
    step: Literal["SECURITY", "AUDIT_CORE", "DI", "COMPLETE"]
    if raw_step in {"SECURITY", "AUDIT_CORE", "DI", "COMPLETE"}:
        step = raw_step  # type: ignore[assignment]
    else:
        step = "SECURITY"
    project_status = "CONFIGURING"
    if operation.audit_core_receipt is not None:
        project_status = str(operation.audit_core_receipt.get("projectStatus") or "CONFIGURING")
    return ProjectProvisioningResponse(
        operationId=UUID(operation.operation_id),
        tenantId=tenant_id,
        projectName=str(summary.get("projectName") or ""),
        projectStatus=project_status,
        provisioningStatus=status,
        currentStep=step,
        errorCode=operation.last_error_code if status == "RECOVERY_REQUIRED" else None,
        errorMessage=operation.last_error_summary if status == "RECOVERY_REQUIRED" else None,
    )


def _mark_recovery(
    engine: Engine,
    *,
    operation: AdministrativeOperation,
    step: str,
    code: str,
    summary: str,
) -> AdministrativeOperation:
    update_administrative_operation(
        engine,
        operation_id=operation.operation_id,
        status="RECOVERY_REQUIRED",
        current_step=step,
        tenant_id=operation.tenant_id,
        last_error_code=code,
        last_error_summary=summary[:500],
    )
    refreshed = get_administrative_operation(
        engine,
        operation_id=operation.operation_id,
        operation_type=_OPERATION_TYPE,
    )
    if refreshed is None:
        raise RuntimeError("Provisioning operation disappeared during recovery update")
    return refreshed


def _resume(
    engine: Engine,
    *,
    operation: AdministrativeOperation,
    request: ProjectCreateRequest,
    admin_request: HumanAdminRequest,
) -> AdministrativeOperation:
    if operation.status == "COMPLETED":
        return operation

    current = operation
    tenant: SecurityTenant
    if current.security_receipt is None:
        update_administrative_operation(
            engine,
            operation_id=current.operation_id,
            status="RUNNING",
            current_step="SECURITY",
        )
        try:
            with SecurityAdminClient(base_url=_security_base_url()) as client:
                tenant = client.create_tenant(
                    human_bearer_token=admin_request.bearer_token,
                    tenant_name=request.projectName.strip(),
                    idempotency_key=current.idempotency_key,
                )
        except SecurityAdminError as exc:
            return _mark_recovery(
                engine,
                operation=current,
                step="SECURITY",
                code="SECURITY_ADMIN_FAILED",
                summary=str(exc),
            )
        update_administrative_operation(
            engine,
            operation_id=current.operation_id,
            tenant_id=tenant.tenant_id,
            status="RUNNING",
            current_step="AUDIT_CORE",
            security_receipt=_security_receipt(tenant),
        )
        current = get_administrative_operation(
            engine, operation_id=current.operation_id, operation_type=_OPERATION_TYPE
        )
        if current is None:
            raise RuntimeError("Provisioning operation disappeared after Security step")
    else:
        receipt = current.security_receipt
        tenant = SecurityTenant(
            tenant_id=str(receipt["tenantId"]),
            tenant_code=str(receipt["tenantCode"]),
            tenant_name=str(receipt["tenantName"]),
            status=str(receipt["status"]),
        )

    if current.audit_core_receipt is None:
        try:
            audit_receipt = _ensure_project_projection(
                engine,
                tenant=tenant,
                request=request,
                actor_id=admin_request.user_id,
            )
        except Exception as exc:  # noqa: BLE001 - durable distributed recovery boundary
            return _mark_recovery(
                engine,
                operation=current,
                step="AUDIT_CORE",
                code="AUDIT_CORE_PROJECTION_FAILED",
                summary=str(exc),
            )
        update_administrative_operation(
            engine,
            operation_id=current.operation_id,
            tenant_id=tenant.tenant_id,
            status="RUNNING",
            current_step="DI",
            audit_core_receipt=audit_receipt,
        )
        current = get_administrative_operation(
            engine, operation_id=current.operation_id, operation_type=_OPERATION_TYPE
        )
        if current is None:
            raise RuntimeError("Provisioning operation disappeared after Audit Core step")

    try:
        with DiClient(base_url=_di_base_url()) as client:
            di_receipt = client.ensure_project_provisioning(
                human_token=admin_request.bearer_token,
                tenant_id=tenant.tenant_id,
                idempotency_key=current.idempotency_key,
            )
    except DiClientError as exc:
        return _mark_recovery(
            engine,
            operation=current,
            step="DI",
            code=exc.code,
            summary=str(exc),
        )
    di_ready = di_receipt.get("provisioningStatus") == "READY"
    update_administrative_operation(
        engine,
        operation_id=current.operation_id,
        tenant_id=tenant.tenant_id,
        status="COMPLETED" if di_ready else "RECOVERY_REQUIRED",
        current_step="COMPLETE" if di_ready else "DI",
        di_receipt=di_receipt,
        last_error_code=None if di_ready else "DI_PROVISIONING_INCOMPLETE",
        last_error_summary=None if di_ready else "DI provisioning is not ready yet.",
        completed=di_ready,
    )
    refreshed = get_administrative_operation(
        engine, operation_id=current.operation_id, operation_type=_OPERATION_TYPE
    )
    if refreshed is None:
        raise RuntimeError("Provisioning operation disappeared after DI step")
    return refreshed


@router.get("/projects", response_model=list[ProjectSelectionResponse])
def list_projects(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> list[ProjectSelectionResponse]:
    with SecurityAdminClient(base_url=_security_base_url()) as client:
        tenants = client.list_tenants(human_bearer_token=admin_request.bearer_token)
    projects = [
        project
        for tenant in tenants
        if (project := _project_selection(engine, tenant=tenant)) is not None
    ]
    return sorted(projects, key=lambda item: (item.projectName.casefold(), item.projectCode))


@router.post("/projects", response_model=ProjectProvisioningResponse)
def create_project(
    request: ProjectCreateRequest,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
    correlation_id: Annotated[str | None, Header(alias="X-Correlation-ID")] = None,
) -> ProjectProvisioningResponse:
    _validate_request(engine, request)
    semantic = _semantic_payload(request)
    with administrative_operation_lock(
        engine,
        operation_type=_OPERATION_TYPE,
        tenant_id=None,
        idempotency_key=idempotency_key,
    ):
        operation = claim_administrative_operation(
            engine,
            operation_type=_OPERATION_TYPE,
            tenant_id=None,
            idempotency_key=idempotency_key,
            semantic_payload=semantic,
            safe_request_summary=semantic,
            initiated_by_user_id=admin_request.user_id,
            correlation_id=correlation_id,
        )
        operation = _resume(
            engine,
            operation=operation,
            request=request,
            admin_request=admin_request,
        )
    response.status_code = 201 if operation.status == "COMPLETED" else 202
    return _response(operation)


@router.get(
    "/project-provisioning-operations/{operation_id}",
    response_model=ProjectProvisioningResponse,
)
def get_project_provisioning_operation(
    operation_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> ProjectProvisioningResponse:
    del admin_request
    operation = get_administrative_operation(
        engine,
        operation_id=str(operation_id),
        operation_type=_OPERATION_TYPE,
    )
    if operation is None:
        raise NotFoundError(
            error_code="VAC-NF-009",
            title="Project provisioning operation not found",
            detail="The requested Project provisioning operation does not exist.",
        )
    return _response(operation)


@router.post(
    "/project-provisioning-operations/{operation_id}/retry",
    response_model=ProjectProvisioningResponse,
)
def retry_project_provisioning_operation(
    operation_id: UUID,
    response: Response,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> ProjectProvisioningResponse:
    operation = get_administrative_operation(
        engine,
        operation_id=str(operation_id),
        operation_type=_OPERATION_TYPE,
    )
    if operation is None:
        raise NotFoundError(
            error_code="VAC-NF-009",
            title="Project provisioning operation not found",
            detail="The requested Project provisioning operation does not exist.",
        )
    if operation.safe_request_summary is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-003",
            title="Project provisioning operation is not recoverable",
            detail="The original Project request receipt is unavailable.",
        )
    request = _request_from_summary(operation.safe_request_summary)
    with administrative_operation_lock(
        engine,
        operation_type=_OPERATION_TYPE,
        tenant_id=None,
        idempotency_key=operation.idempotency_key,
    ):
        operation = _resume(
            engine,
            operation=operation,
            request=request,
            admin_request=admin_request,
        )
    response.status_code = 200 if operation.status == "COMPLETED" else 202
    return _response(operation)
