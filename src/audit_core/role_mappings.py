from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.errors import NotFoundError
from audit_core.security_integration import SecurityAdminClient

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["role-mapping"])


class RoleMappingCandidateResponse(BaseModel):
    userId: str
    displayName: str
    primaryEmail: str | None
    status: str


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


@router.get(
    "/role-mapping-candidates",
    response_model=list[RoleMappingCandidateResponse],
)
def list_role_mapping_candidates(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    q: str | None = Query(default=None, max_length=320),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[RoleMappingCandidateResponse]:
    set_tenant_context(connection, tenant_id)
    _require_project(connection, tenant_id)

    security_base_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    if not security_base_url:
        raise RuntimeError("SECURITY_BASE_URL is required for UC02 administration")

    with SecurityAdminClient(base_url=security_base_url) as client:
        users = client.list_global_users(
            human_bearer_token=admin_request.bearer_token,
            search=q,
            limit=limit,
        )

    return [
        RoleMappingCandidateResponse(
            userId=user.user_id,
            displayName=user.display_name,
            primaryEmail=user.primary_email,
            status=user.status,
        )
        for user in users
    ]
