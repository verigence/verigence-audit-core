from __future__ import annotations

import os
from datetime import date
from typing import Annotated, Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import structlog
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import (
    BusinessValidationError,
    ConflictError,
    DependencyUnavailableError,
)
from audit_core.security_integration import (
    SecurityAdminClient,
    SecurityAdminError,
    SecurityTenant,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["project-provisioning"])

_SECURITY_ADMIN_PROVISIONING_TIMEOUT_SECONDS = 20.0
_COMPENSATION_TIMEOUT_SECONDS = 20.0


class ProjectCreateRequest(BaseModel):
    projectName: str = Field(min_length=1, max_length=240)
    oemId: UUID
    segmentIds: list[UUID] = Field(default_factory=list)
    # Legacy compatibility only. Product Category is no longer a Project-onboarding input.
    productCategoryId: UUID | None = None
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


def _validate_request(engine: Engine, request: ProjectCreateRequest) -> None:
    if (
        request.effectiveEndDate is not None
        and request.effectiveEndDate < request.effectiveStartDate
    ):
        raise BusinessValidationError(
            detail="Effective End Date cannot be earlier than Effective Start Date."
        )
    if len(set(request.segmentIds)) != len(request.segmentIds):
        raise BusinessValidationError(detail="Project Segments cannot contain duplicates.")

    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        oem_exists = connection.execute(
            text("SELECT 1 FROM auditcore.oems WHERE oem_id=:oem_id AND is_active=true"),
            {"oem_id": request.oemId},
        ).scalar_one_or_none()
        if oem_exists is None:
            raise BusinessValidationError(detail="OEM does not reference an active approved value.")

        configured_segment_ids = set(
            connection.execute(
                text(
                    """
                    SELECT segment_id
                    FROM auditcore.oem_segments
                    WHERE oem_id=:oem_id AND is_active=true
                    """
                ),
                {"oem_id": request.oemId},
            ).scalars().all()
        )
        selected_segment_ids = set(request.segmentIds)
        if configured_segment_ids and not selected_segment_ids:
            raise BusinessValidationError(
                detail="Select at least one Segment configured for the chosen OEM."
            )
        if not selected_segment_ids.issubset(configured_segment_ids):
            raise BusinessValidationError(
                detail="Every selected Segment must belong to the chosen OEM and be active."
            )


def _project_exists(engine: Engine, tenant_id: str) -> bool:
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        set_tenant_context(connection, tenant_id)
        return (
            connection.execute(
                text("SELECT 1 FROM auditcore.projects WHERE tenant_id=:tenant_id"),
                {"tenant_id": tenant_id},
            ).scalar_one_or_none()
            is not None
        )


def _ensure_project_segments(
    connection: Connection,
    *,
    tenant_id: str,
    segment_ids: list[UUID],
    actor_id: str,
) -> None:
    current = set(
        connection.execute(
            text(
                "SELECT segment_id FROM auditcore.project_segments WHERE tenant_id=:tenant_id"
            ),
            {"tenant_id": tenant_id},
        ).scalars().all()
    )
    requested = set(segment_ids)
    if current and current != requested:
        raise ConflictError(
            error_code="VAC-CONFLICT-003",
            title="Project provisioning conflict",
            detail="The Project context is already linked to different Segment selections.",
        )
    if current == requested:
        return
    for segment_id in segment_ids:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.project_segments (
                    tenant_id, segment_id, created_by_actor_id
                ) VALUES (:tenant_id, :segment_id, :actor_id)
                ON CONFLICT (tenant_id, segment_id) DO NOTHING
                """
            ),
            {"tenant_id": tenant_id, "segment_id": segment_id, "actor_id": actor_id},
        )


def _ensure_project_projection(
    connection: Connection,
    *,
    tenant: SecurityTenant,
    request: ProjectCreateRequest,
    actor_id: str,
) -> dict[str, Any]:
    connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
    set_tenant_context(connection, tenant.tenant_id)
    current = connection.execute(
        text(
            """
            SELECT tenant_id, project_code, project_name, oem_id,
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
                    :tenant_id, :project_code, :project_name, :oem_id, NULL,
                    :effective_start, :effective_end, :timezone_name, :region_code,
                    'CONFIGURING', :actor_id, :actor_id
                )
                RETURNING tenant_id, project_code, project_name, oem_id,
                          effective_start_date, effective_end_date, timezone_name, region_code,
                          project_status
                """
            ),
            {
                "tenant_id": tenant.tenant_id,
                "project_code": tenant.tenant_code,
                "project_name": request.projectName.strip(),
                "oem_id": request.oemId,
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
        "effective_start_date": request.effectiveStartDate,
        "effective_end_date": request.effectiveEndDate,
        "timezone_name": request.timezoneName.strip(),
        "region_code": request.regionCode.strip() if request.regionCode else None,
    }
    if any(current[key] != value for key, value in expected.items()):
        raise ConflictError(
            error_code="VAC-CONFLICT-003",
            title="Project provisioning conflict",
            detail="The Project context is already linked to different Project details.",
        )

    _ensure_project_segments(
        connection,
        tenant_id=tenant.tenant_id,
        segment_ids=request.segmentIds,
        actor_id=actor_id,
    )
    return {
        "tenantId": str(current["tenant_id"]),
        "projectCode": str(current["project_code"]),
        "projectStatus": str(current["project_status"]),
    }


def _delete_di_provisioning(*, base_url: str, token: str, tenant_id: str) -> None:
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=_COMPENSATION_TIMEOUT_SECONDS,
    ) as client:
        response = client.delete(
            f"/v1/tenants/{tenant_id}/admin/provisioning",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError("DI provisioning compensation failed")


def _delete_security_tenant(*, base_url: str, token: str, tenant_id: str) -> None:
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=_COMPENSATION_TIMEOUT_SECONDS,
    ) as client:
        response = client.delete(
            f"/security/v1/platform/tenants/{tenant_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code == 404:
        return
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError("Security Tenant compensation failed")


def _compensate_new_project(
    *,
    tenant_id: str,
    human_token: str,
    di_cleanup_required: bool,
) -> None:
    if di_cleanup_required:
        _delete_di_provisioning(
            base_url=_di_base_url(),
            token=human_token,
            tenant_id=tenant_id,
        )
    _delete_security_tenant(
        base_url=_security_base_url(),
        token=human_token,
        tenant_id=tenant_id,
    )


@router.get("/projects", response_model=list[ProjectSelectionResponse])
def list_projects(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> list[ProjectSelectionResponse]:
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        set_platform_super_admin_context(connection)
        rows = connection.execute(
            text(
                """
                SELECT tenant_id, project_code, project_name, project_status
                FROM auditcore.projects
                ORDER BY lower(project_name), project_code
                """
            )
        ).mappings().all()

    return [
        ProjectSelectionResponse(
            tenantId=str(row["tenant_id"]),
            projectCode=str(row["project_code"]),
            projectName=str(row["project_name"]),
            projectStatus=str(row["project_status"]),
            securityTenantStatus="NOT_QUERIED",
        )
        for row in rows
    ]


@router.post("/projects", response_model=ProjectProvisioningResponse, status_code=201)
def create_project(
    request: ProjectCreateRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> ProjectProvisioningResponse:
    _validate_request(engine, request)

    try:
        security_url = _security_base_url()
        di_url = _di_base_url()
        with SecurityAdminClient(
            base_url=security_url,
            timeout_seconds=_SECURITY_ADMIN_PROVISIONING_TIMEOUT_SECONDS,
        ) as client:
            tenant = client.create_tenant(
                human_bearer_token=admin_request.bearer_token,
                tenant_name=request.projectName.strip(),
                idempotency_key=idempotency_key,
            )
    except (SecurityAdminError, RuntimeError) as exc:
        logger.warning("project_create_security_failed", exc_type=type(exc).__name__)
        raise DependencyUnavailableError(
            detail=f"{request.projectName.strip()} setup could not be completed. Please try again."
        ) from exc

    existing_project = _project_exists(engine, tenant.tenant_id)
    di_cleanup_required = False
    connection = engine.connect()
    transaction = connection.begin()
    try:
        projection = _ensure_project_projection(
            connection,
            tenant=tenant,
            request=request,
            actor_id=admin_request.user_id,
        )
        di_cleanup_required = True
        with DiClient(base_url=di_url) as client:
            di_result = client.ensure_project_provisioning(
                human_token=admin_request.bearer_token,
                tenant_id=tenant.tenant_id,
                idempotency_key=idempotency_key,
            )
        if di_result.get("provisioningStatus") != "READY":
            raise RuntimeError("DI provisioning did not reach READY state")
        transaction.commit()
    except Exception as exc:
        if transaction.is_active:
            transaction.rollback()
        logger.error(
            "project_create_failed",
            tenant_id=tenant.tenant_id,
            exc_type=type(exc).__name__,
        )
        if not existing_project:
            try:
                _compensate_new_project(
                    tenant_id=tenant.tenant_id,
                    human_token=admin_request.bearer_token,
                    di_cleanup_required=di_cleanup_required,
                )
            except Exception as compensation_exc:
                logger.critical(
                    "project_create_compensation_failed",
                    tenant_id=tenant.tenant_id,
                    exc_type=type(compensation_exc).__name__,
                )
                raise DependencyUnavailableError(
                    detail=(
                        f"{request.projectName.strip()} setup could not be completed cleanly. "
                        "Please contact support before trying again."
                    )
                ) from compensation_exc
        if isinstance(exc, (BusinessValidationError, ConflictError)):
            raise
        if isinstance(exc, DiClientError):
            raise DependencyUnavailableError(
                detail=f"{request.projectName.strip()} setup could not be completed. Please try again."
            ) from exc
        raise DependencyUnavailableError(
            detail=f"{request.projectName.strip()} setup could not be completed. Please try again."
        ) from exc
    finally:
        connection.close()

    operation_id = uuid5(
        NAMESPACE_URL,
        f"verigence:uc02:project-create:{admin_request.user_id}:{idempotency_key}",
    )
    return ProjectProvisioningResponse(
        operationId=operation_id,
        tenantId=tenant.tenant_id,
        projectName=request.projectName.strip(),
        projectStatus=str(projection["projectStatus"]),
        provisioningStatus="READY",
        currentStep="COMPLETE",
        errorCode=None,
        errorMessage=None,
    )
