from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.authorization import require_tenant
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}/project", tags=["project"])


class ProjectResponse(BaseModel):
    tenantId: str
    projectCode: str
    projectName: str


class ProjectPatch(BaseModel):
    projectName: str = Field(min_length=1, max_length=240)


def _not_found() -> NotFoundError:
    return NotFoundError(
        error_code="VAC-NF-001",
        title="Project not found",
        detail="Project not found for the requested tenant.",
    )


def _project(connection: Connection, tenant_id: str) -> ProjectResponse:
    row = connection.execute(
        text(
            """
            SELECT tenant_id, project_code, project_name
            FROM auditcore.projects
            WHERE tenant_id = :tenant_id
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        raise _not_found()
    return ProjectResponse(
        tenantId=row["tenant_id"],
        projectCode=row["project_code"],
        projectName=row["project_name"],
    )


@router.get("", response_model=ProjectResponse)
def get_project(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
    connection: Connection = Depends(get_connection),
) -> ProjectResponse:
    require_tenant(principal, tenant_id)
    set_tenant_context(connection, tenant_id)
    return _project(connection, tenant_id)


@router.patch("", response_model=ProjectResponse)
def patch_project(
    tenant_id: str,
    patch: ProjectPatch,
    principal: Principal = Depends(get_principal),
    connection: Connection = Depends(get_connection),
) -> ProjectResponse:
    require_tenant(principal, tenant_id)
    set_tenant_context(connection, tenant_id)
    row = connection.execute(
        text(
            """
            UPDATE auditcore.projects
            SET project_name = :project_name,
                updated_by_actor_id = :actor_id,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id
            RETURNING tenant_id, project_code, project_name
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_name": patch.projectName,
            "actor_id": principal.subject,
        },
    ).mappings().one_or_none()
    if row is None:
        raise _not_found()
    return ProjectResponse(
        tenantId=row["tenant_id"],
        projectCode=row["project_code"],
        projectName=row["project_name"],
    )
