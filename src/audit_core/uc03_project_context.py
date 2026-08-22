from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_security_actor_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal

router = APIRouter(prefix="/v1/me", tags=["uc03-project-context"])


class ProjectScopeSummary(BaseModel):
    allDealers: bool
    dealerCount: int
    outletCount: int


class OperationalProject(BaseModel):
    tenantId: str
    projectCode: str
    projectName: str
    projectStatus: str
    timezoneName: str
    operatingRole: str
    scope: ProjectScopeSummary


class MyProjectsResponse(BaseModel):
    projects: list[OperationalProject]


@router.get("/projects", response_model=MyProjectsResponse)
def list_my_projects(
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> MyProjectsResponse:
    """Return active operational Projects for the authenticated global human USER.

    This is deliberately distinct from the SuperAdmin /v1/projects control-plane API.
    The global Security token contributes identity only; Project role/scope is resolved
    from Audit Core's active business_assignments projection.
    """

    set_security_actor_context(connection, human_principal.subject)
    rows = list(
        connection.execute(
            text(
                """
                SELECT
                    p.tenant_id,
                    p.project_code,
                    p.project_name,
                    p.project_status,
                    p.timezone_name,
                    min(ba.business_role_code) AS operating_role,
                    count(DISTINCT ba.business_role_code) AS operating_role_count,
                    bool_or(ba.dealer_id IS NULL AND ba.outlet_id IS NULL) AS all_dealers,
                    count(DISTINCT ba.dealer_id) AS dealer_count,
                    count(DISTINCT ba.outlet_id) AS outlet_count
                FROM auditcore.projects p
                JOIN auditcore.business_assignments ba
                  ON ba.tenant_id = p.tenant_id
                WHERE ba.security_actor_id = :actor_id
                  AND ba.assignment_status = 'ACTIVE'
                  AND ba.effective_from <= now()
                  AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                  AND p.project_status = 'ACTIVE'
                GROUP BY
                    p.tenant_id,
                    p.project_code,
                    p.project_name,
                    p.project_status,
                    p.timezone_name
                ORDER BY lower(p.project_name), p.project_code, p.tenant_id
                """
            ),
            {"actor_id": human_principal.subject},
        ).mappings()
    )

    projects: list[OperationalProject] = []
    for row in rows:
        if int(row["operating_role_count"]) != 1:
            # UC02 Role Mapping guarantees one operating role per USER/Project. Do not
            # guess when the projection is inconsistent; the global exception handler
            # converts this into a safe correlation-bearing platform error.
            raise RuntimeError("UC03 Project context has inconsistent operating roles")
        projects.append(
            OperationalProject(
                tenantId=str(row["tenant_id"]),
                projectCode=str(row["project_code"]),
                projectName=str(row["project_name"]),
                projectStatus=str(row["project_status"]),
                timezoneName=str(row["timezone_name"]),
                operatingRole=str(row["operating_role"]),
                scope=ProjectScopeSummary(
                    allDealers=bool(row["all_dealers"]),
                    dealerCount=int(row["dealer_count"]),
                    outletCount=int(row["outlet_count"]),
                ),
            )
        )
    return MyProjectsResponse(projects=projects)
