from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

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


@router.get("/projects", response_model=MyProjectsResponse)
def list_my_projects(
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> MyProjectsResponse:
    """Return active operational Projects and the actor's concrete working scope.

    Actor RLS context, Project, role and PC Outlet scope are deliberately resolved in
    one PostgreSQL statement. The MATERIALIZED runtime_context CTE is a dependency of
    every protected read, so the validated Security actor is established before RLS
    evaluates Projects, assignments, Dealers or Outlets. No cross-Tenant write access
    or broad Dealer/Outlet visibility is introduced.
    """

    rows = list(
        connection.execute(
            text(
                """
                WITH runtime_context AS MATERIALIZED (
                    SELECT set_config('app.security_actor_id', :actor_id, true) AS actor_context
                ),
                active_assignments AS MATERIALIZED (
                    SELECT
                        ba.tenant_id,
                        ba.business_role_code,
                        ba.dealer_id,
                        ba.outlet_id
                    FROM runtime_context rc
                    CROSS JOIN auditcore.business_assignments ba
                    WHERE ba.security_actor_id = :actor_id
                      AND ba.assignment_status = 'ACTIVE'
                      AND ba.effective_from <= now()
                      AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                ),
                project_scope AS (
                    SELECT
                        p.tenant_id,
                        p.project_code,
                        p.project_name,
                        p.project_status,
                        p.timezone_name,
                        min(a.business_role_code) AS operating_role,
                        count(DISTINCT a.business_role_code) AS operating_role_count,
                        bool_or(a.dealer_id IS NULL AND a.outlet_id IS NULL) AS all_dealers,
                        count(DISTINCT a.dealer_id) AS dealer_count,
                        count(DISTINCT a.outlet_id) AS outlet_count
                    FROM runtime_context rc
                    CROSS JOIN auditcore.projects p
                    JOIN active_assignments a
                      ON a.tenant_id = p.tenant_id
                    WHERE p.project_status = 'ACTIVE'
                    GROUP BY
                        p.tenant_id,
                        p.project_code,
                        p.project_name,
                        p.project_status,
                        p.timezone_name
                ),
                pc_outlet_rows AS (
                    SELECT DISTINCT
                        a.tenant_id,
                        a.dealer_id,
                        d.dealer_name,
                        a.outlet_id,
                        o.outlet_name,
                        o.outlet_classification
                    FROM active_assignments a
                    JOIN auditcore.dealers d
                      ON d.tenant_id = a.tenant_id
                     AND d.dealer_id = a.dealer_id
                    JOIN auditcore.dealer_outlets o
                      ON o.tenant_id = a.tenant_id
                     AND o.dealer_id = a.dealer_id
                     AND o.outlet_id = a.outlet_id
                    WHERE a.business_role_code = 'PC'
                      AND a.dealer_id IS NOT NULL
                      AND a.outlet_id IS NOT NULL
                      AND d.status = 'ACTIVE'
                      AND o.status = 'ACTIVE'
                ),
                pc_outlets AS (
                    SELECT
                        tenant_id,
                        jsonb_agg(
                            jsonb_build_object(
                                'dealerId', dealer_id::text,
                                'dealerName', dealer_name,
                                'outletId', outlet_id::text,
                                'outletName', outlet_name,
                                'outletClassification', outlet_classification
                            )
                            ORDER BY lower(dealer_name), lower(outlet_name), outlet_id
                        ) AS outlets
                    FROM pc_outlet_rows
                    GROUP BY tenant_id
                )
                SELECT
                    ps.tenant_id,
                    ps.project_code,
                    ps.project_name,
                    ps.project_status,
                    ps.timezone_name,
                    ps.operating_role,
                    ps.operating_role_count,
                    ps.all_dealers,
                    ps.dealer_count,
                    ps.outlet_count,
                    COALESCE(po.outlets, '[]'::jsonb) AS outlets
                FROM project_scope ps
                LEFT JOIN pc_outlets po
                  ON po.tenant_id = ps.tenant_id
                ORDER BY lower(ps.project_name), ps.project_code, ps.tenant_id
                """
            ),
            {"actor_id": human_principal.subject},
        ).mappings()
    )

    projects: list[OperationalProject] = []
    for row in rows:
        if int(row["operating_role_count"]) != 1:
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
                    outlets=[OperationalOutletScope.model_validate(item) for item in row["outlets"]],
                ),
            )
        )
    return MyProjectsResponse(projects=projects)
