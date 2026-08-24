from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_security_actor_context, set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal

router = APIRouter(prefix="/v1/me", tags=["uc03-project-context"])


class OperationalOutletScope(BaseModel):
    dealerId: UUID
    dealerName: str
    outletId: UUID
    outletName: str
    outletClassification: str


class ProjectScopeSummary(BaseModel):
    allDealers: bool
    dealerCount: int
    outletCount: int
    outlets: list[OperationalOutletScope]


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


def _pc_outlets(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
) -> list[OperationalOutletScope]:
    """Return the concrete active Outlet scopes already granted to this PC.

    Project discovery resolves authorization once. For PC users we retain the real
    Dealer/Outlet assignments in that response so the Web can establish the working
    Outlet before opening the operational dashboard.
    """

    set_tenant_context(connection, tenant_id)
    rows = list(
        connection.execute(
            text(
                """
                SELECT
                    ba.dealer_id,
                    d.dealer_name,
                    ba.outlet_id,
                    o.outlet_name,
                    o.outlet_classification
                FROM auditcore.business_assignments ba
                JOIN auditcore.dealers d
                  ON d.tenant_id = ba.tenant_id
                 AND d.dealer_id = ba.dealer_id
                JOIN auditcore.dealer_outlets o
                  ON o.tenant_id = ba.tenant_id
                 AND o.dealer_id = ba.dealer_id
                 AND o.outlet_id = ba.outlet_id
                WHERE ba.tenant_id = :tenant_id
                  AND ba.security_actor_id = :actor_id
                  AND ba.business_role_code = 'PC'
                  AND ba.assignment_status = 'ACTIVE'
                  AND ba.effective_from <= now()
                  AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                  AND ba.dealer_id IS NOT NULL
                  AND ba.outlet_id IS NOT NULL
                  AND d.status = 'ACTIVE'
                  AND o.status = 'ACTIVE'
                ORDER BY lower(d.dealer_name), lower(o.outlet_name), o.outlet_id
                """
            ),
            {"tenant_id": tenant_id, "actor_id": actor_id},
        ).mappings()
    )

    seen: set[UUID] = set()
    outlets: list[OperationalOutletScope] = []
    for row in rows:
        outlet_id = UUID(str(row["outlet_id"]))
        if outlet_id in seen:
            continue
        seen.add(outlet_id)
        outlets.append(
            OperationalOutletScope(
                dealerId=UUID(str(row["dealer_id"])),
                dealerName=str(row["dealer_name"]),
                outletId=outlet_id,
                outletName=str(row["outlet_name"]),
                outletClassification=str(row["outlet_classification"]),
            )
        )
    return outlets


@router.get("/projects", response_model=MyProjectsResponse)
def list_my_projects(
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> MyProjectsResponse:
    """Return active operational Projects and the actor's concrete working scope.

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
            raise RuntimeError("UC03 Project context has inconsistent operating roles")

        tenant_id = str(row["tenant_id"])
        operating_role = str(row["operating_role"])
        outlets = (
            _pc_outlets(
                connection,
                tenant_id=tenant_id,
                actor_id=human_principal.subject,
            )
            if operating_role.upper() == "PC"
            else []
        )
        projects.append(
            OperationalProject(
                tenantId=tenant_id,
                projectCode=str(row["project_code"]),
                projectName=str(row["project_name"]),
                projectStatus=str(row["project_status"]),
                timezoneName=str(row["timezone_name"]),
                operatingRole=operating_role,
                scope=ProjectScopeSummary(
                    allDealers=bool(row["all_dealers"]),
                    dealerCount=int(row["dealer_count"]),
                    outletCount=int(row["outlet_count"]),
                    outlets=outlets,
                ),
            )
        )
    return MyProjectsResponse(projects=projects)
