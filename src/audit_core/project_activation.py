from __future__ import annotations

import os
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.errors import AuditCoreError, ConflictError, NotFoundError
from audit_core.readiness import ProjectReadinessResponse, evaluate_project_readiness
from audit_core.security_integration import SecurityAdminClient, SecurityAdminError

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/tenants/{tenant_id}/project", tags=["project-activation"])

_SECURITY_COMPENSATION_TIMEOUT_SECONDS = 20.0


class ProjectActivationResponse(BaseModel):
    tenantId: str
    projectStatus: str
    securityTenantStatus: str
    readiness: ProjectReadinessResponse


def _security_base_url() -> str:
    value = os.environ.get("SECURITY_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("SECURITY_BASE_URL is required for UC02 administration")
    return value


def _project_status(connection: Connection, tenant_id: str) -> str:
    status = connection.execute(
        text(
            "SELECT project_status FROM auditcore.projects "
            "WHERE tenant_id=:tenant_id"
        ),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    if status is None:
        raise NotFoundError(
            error_code="VAC-NF-001",
            title="Project not found",
            detail="Project not found for the requested tenant.",
        )
    return str(status)


def _restore_security_configuring(*, base_url: str, token: str, tenant_id: str) -> None:
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=_SECURITY_COMPENSATION_TIMEOUT_SECONDS,
    ) as client:
        response = client.post(
            f"/security/v1/platform/tenants/{tenant_id}/restore-configuring",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError("Security activation compensation failed")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Security activation compensation returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != "CONFIGURING":
        raise RuntimeError("Security activation compensation did not restore CONFIGURING")


def _activation_error(detail: str) -> AuditCoreError:
    return AuditCoreError(
        error_code="VAC-SYS-001",
        status_code=503,
        title="Project activation unavailable",
        detail=detail,
    )


def _compensate_activation_or_raise(
    *,
    tenant_id: str,
    admin_request: HumanAdminRequest,
    original_error: Exception,
) -> None:
    try:
        _restore_security_configuring(
            base_url=_security_base_url(),
            token=admin_request.bearer_token,
            tenant_id=tenant_id,
        )
    except Exception as compensation_exc:
        logger.critical(
            "project_activation_compensation_failed",
            tenant_id=tenant_id,
            exc_type=type(compensation_exc).__name__,
        )
        raise _activation_error(
            "Project activation could not be completed cleanly. Please contact support."
        ) from compensation_exc
    raise _activation_error("Project activation could not be completed. Please try again.") from original_error


@router.post("/activate", response_model=ProjectActivationResponse)
def activate_project(
    tenant_id: str,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> ProjectActivationResponse:
    # Idempotency is natural for the final ACTIVE state; no recovery workflow is used.
    del idempotency_key

    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        set_tenant_context(connection, tenant_id)
        current_status = _project_status(connection, tenant_id)

        if current_status == "ACTIVE":
            try:
                with SecurityAdminClient(base_url=_security_base_url()) as client:
                    tenant = client.get_tenant(
                        human_bearer_token=admin_request.bearer_token,
                        tenant_id=tenant_id,
                    )
            except SecurityAdminError as exc:
                raise _activation_error(
                    "Project activation could not be verified. Please try again."
                ) from exc
            if tenant.status != "ACTIVE":
                raise ConflictError(
                    error_code="VAC-CONFLICT-001",
                    title="Project activation state is inconsistent",
                    detail="Project activation could not be verified. Please contact support.",
                )
            readiness = evaluate_project_readiness(
                tenant_id=tenant_id,
                admin_request=admin_request,
                connection=connection,
            )
            transaction.rollback()
            return ProjectActivationResponse(
                tenantId=tenant_id,
                projectStatus="ACTIVE",
                securityTenantStatus=tenant.status,
                readiness=readiness,
            )

        readiness = evaluate_project_readiness(
            tenant_id=tenant_id,
            admin_request=admin_request,
            connection=connection,
        )
        if not readiness.readyToActivate:
            failed = [
                check.checkKey
                for check in readiness.checks
                if check.severity == "BLOCKING" and check.status != "PASS"
            ]
            raise ConflictError(
                error_code="VAC-CONFLICT-001",
                title="Project is not ready for activation",
                detail=(
                    "Resolve blocking Project Readiness checks before activation: "
                    + ", ".join(failed)
                    + "."
                ),
            )

        # Keep the Audit Core ACTIVE update uncommitted until Security reports ACTIVE.
        connection.execute(
            text(
                """
                UPDATE auditcore.projects
                SET project_status='ACTIVE',
                    updated_by_actor_id=:actor_id,
                    updated_at_utc=now(),
                    version_no=version_no + 1
                WHERE tenant_id=:tenant_id AND project_status<>'ACTIVE'
                """
            ),
            {"tenant_id": tenant_id, "actor_id": admin_request.user_id},
        )

        try:
            with SecurityAdminClient(base_url=_security_base_url()) as client:
                tenant = client.activate_tenant(
                    human_bearer_token=admin_request.bearer_token,
                    tenant_id=tenant_id,
                )
        except SecurityAdminError as exc:
            if transaction.is_active:
                transaction.rollback()
            # The activation request may have committed in Security before its response
            # was lost. The compensation endpoint is idempotent for CONFIGURING, so
            # compensate on every uncertain activation result.
            _compensate_activation_or_raise(
                tenant_id=tenant_id,
                admin_request=admin_request,
                original_error=exc,
            )

        if tenant.status != "ACTIVE":
            if transaction.is_active:
                transaction.rollback()
            _compensate_activation_or_raise(
                tenant_id=tenant_id,
                admin_request=admin_request,
                original_error=RuntimeError("Security activation did not reach ACTIVE"),
            )

        try:
            transaction.commit()
        except Exception as exc:
            if transaction.is_active:
                transaction.rollback()
            _compensate_activation_or_raise(
                tenant_id=tenant_id,
                admin_request=admin_request,
                original_error=exc,
            )

        return ProjectActivationResponse(
            tenantId=tenant_id,
            projectStatus="ACTIVE",
            securityTenantStatus=tenant.status,
            readiness=readiness,
        )
    except Exception:
        if transaction.is_active:
            transaction.rollback()
        raise
    finally:
        connection.close()
