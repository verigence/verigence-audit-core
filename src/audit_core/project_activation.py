from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.errors import AuditCoreError, ConflictError, NotFoundError
from audit_core.readiness import ProjectReadinessResponse, evaluate_project_readiness
from audit_core.security_integration import SecurityAdminClient, SecurityAdminError

router = APIRouter(prefix="/v1/tenants/{tenant_id}/project", tags=["project-activation"])


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


@router.post("/activate", response_model=ProjectActivationResponse)
def activate_project(
    tenant_id: str,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectActivationResponse:
    # The lifecycle operation is naturally idempotent; the required key is retained at
    # the browser contract boundary and Security activation is itself retried against
    # the same canonical Tenant rather than creating a second resource.
    del idempotency_key
    set_tenant_context(connection, tenant_id)
    _project_status(connection, tenant_id)

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

    try:
        with SecurityAdminClient(base_url=_security_base_url()) as client:
            tenant = client.activate_tenant(
                human_bearer_token=admin_request.bearer_token,
                tenant_id=tenant_id,
            )
    except SecurityAdminError:
        # Do not retain/chains the downstream exception. Administrative dependency
        # details may contain implementation context and Audit Core's public problem
        # response intentionally exposes only the stable dependency failure contract.
        raise AuditCoreError(
            error_code="VAC-SYS-001",
            status_code=503,
            title="Security activation unavailable",
            detail="Security could not activate the Project Tenant. Audit Core remains unchanged.",
        )

    if tenant.status != "ACTIVE":
        raise ConflictError(
            error_code="VAC-CONFLICT-001",
            title="Security Tenant is not active",
            detail=(
                "Security did not confirm ACTIVE Tenant state. "
                "Audit Core remains unchanged."
            ),
        )

    connection.execute(
        text(
            """
            UPDATE auditcore.projects
            SET project_status='ACTIVE',
                updated_by_actor_id=:actor_id,
                updated_at_utc=now(),
                version_no=CASE
                    WHEN project_status='ACTIVE' THEN version_no
                    ELSE version_no + 1
                END
            WHERE tenant_id=:tenant_id
            """
        ),
        {"tenant_id": tenant_id, "actor_id": admin_request.user_id},
    )

    return ProjectActivationResponse(
        tenantId=tenant_id,
        projectStatus="ACTIVE",
        securityTenantStatus=tenant.status,
        readiness=readiness,
    )