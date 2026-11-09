"""Read-only feed of service-audit Projects for SevaSutra (workshop audit).

Verigence is central (product owner, 9 Nov 2026): a service-audit Project, its Dealers and Outlets, and
who works on it in which role and at which Outlets, are set up in Verigence (Project Administration).
SevaSutra reads this feed with its ServiceIntegration token and keeps its dealerships, outlets and each
person's outlet scope in step; it never creates them itself. This endpoint only reads: no tables, no
writes, no existing code path touched. SevaSutra calls it every few minutes with a short timeout.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import get_engine
from audit_core.errors import AuditCoreError
from audit_core.security import ServiceIntegrationPrincipal
from audit_core.uc03_pc_booking_documents import require_audit_service_principal

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["SevaSutra feed"])

# Only SevaSutra's service identity may read this.
_SEVASUTRA_SERVICE_SUBJECT = "sevasutra"
_STATEMENT_TIMEOUT = "5s"


class SevaSutraDealer(BaseModel):
    dealerId: UUID
    dealerCode: str
    dealerName: str
    status: str


class SevaSutraOutlet(BaseModel):
    outletId: UUID
    dealerId: UUID
    outletCode: str | None
    outletName: str
    city: str | None
    status: str


class SevaSutraAssignment(BaseModel):
    securityUserId: str
    roleCode: str
    # Empty for a Project-wide role; the Outlets for an Outlet-scoped one (ANALYST, PC).
    outletIds: list[UUID]
    dealerIds: list[UUID]


class SevaSutraProject(BaseModel):
    tenantId: str
    projectCode: str
    projectName: str
    projectStatus: str
    oemCode: str | None
    oemName: str | None
    timezoneName: str | None
    effectiveStartDate: date | None
    dealers: list[SevaSutraDealer]
    outlets: list[SevaSutraOutlet]
    assignments: list[SevaSutraAssignment]


class SevaSutraFeedResponse(BaseModel):
    generatedAt: datetime
    projects: list[SevaSutraProject]


_PROJECTS_SQL = text(
    """
    SELECT p.tenant_id, p.project_code, p.project_name, p.project_status, p.timezone_name,
           p.effective_start_date, o.oem_code, o.oem_name
    FROM auditcore.projects p
    LEFT JOIN auditcore.oems o ON o.oem_id = p.oem_id
    WHERE p.audit_type = 'SERVICE' AND p.project_status IN ('ACTIVE', 'CONFIGURING')
    ORDER BY p.tenant_id
    """
)

_DEALERS_SQL = text(
    """
    SELECT dealer_id, dealer_code, dealer_name, status
    FROM auditcore.dealers WHERE tenant_id = :tenant_id ORDER BY dealer_name
    """
)

_OUTLETS_SQL = text(
    """
    SELECT outlet_id, dealer_id, outlet_code, outlet_name, city, status
    FROM auditcore.dealer_outlets WHERE tenant_id = :tenant_id ORDER BY outlet_name
    """
)

_ASSIGNMENTS_SQL = text(
    """
    SELECT security_actor_id, business_role_code, dealer_id, outlet_id
    FROM auditcore.business_assignments
    WHERE tenant_id = :tenant_id
      AND assignment_status = 'ACTIVE'
      AND effective_from <= now()
      AND (effective_to IS NULL OR effective_to > now())
    ORDER BY security_actor_id
    """
)


def _assignments(rows: list[Any]) -> list[SevaSutraAssignment]:
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = grouped.setdefault(
            str(r["security_actor_id"]),
            {"role": str(r["business_role_code"]), "outlets": set(), "dealers": set()},
        )
        if r["outlet_id"] is not None:
            g["outlets"].add(UUID(str(r["outlet_id"])))
        elif r["dealer_id"] is not None:
            g["dealers"].add(UUID(str(r["dealer_id"])))
    return [
        SevaSutraAssignment(
            securityUserId=user,
            roleCode=g["role"],
            outletIds=sorted(g["outlets"], key=str),
            dealerIds=sorted(g["dealers"], key=str),
        )
        for user, g in grouped.items()
    ]


def load_service_projects(engine: Engine) -> list[SevaSutraProject]:
    """Every service-audit Project with its Dealers, Outlets and role assignments, one read per Project."""
    with engine.begin() as connection:
        connection.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
        set_platform_super_admin_context(connection)
        projects = connection.execute(_PROJECTS_SQL).mappings().all()
    out: list[SevaSutraProject] = []
    for p in projects:
        tenant_id = str(p["tenant_id"])
        with engine.begin() as connection:
            connection.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
            set_tenant_context(connection, tenant_id)
            dealers = connection.execute(_DEALERS_SQL, {"tenant_id": tenant_id}).mappings().all()
            outlets = connection.execute(_OUTLETS_SQL, {"tenant_id": tenant_id}).mappings().all()
            assignments = connection.execute(_ASSIGNMENTS_SQL, {"tenant_id": tenant_id}).mappings().all()
        out.append(
            SevaSutraProject(
                tenantId=tenant_id,
                projectCode=str(p["project_code"]),
                projectName=str(p["project_name"]),
                projectStatus=str(p["project_status"]),
                oemCode=p["oem_code"],
                oemName=p["oem_name"],
                timezoneName=p["timezone_name"],
                effectiveStartDate=p["effective_start_date"],
                dealers=[
                    SevaSutraDealer(dealerId=d["dealer_id"], dealerCode=str(d["dealer_code"]), dealerName=str(d["dealer_name"]), status=str(d["status"]))
                    for d in dealers
                ],
                outlets=[
                    SevaSutraOutlet(outletId=o["outlet_id"], dealerId=o["dealer_id"], outletCode=o["outlet_code"], outletName=str(o["outlet_name"]), city=o["city"], status=str(o["status"]))
                    for o in outlets
                ],
                assignments=_assignments(list(assignments)),
            )
        )
    return out


@router.get("/v1/service/sevasutra/projects", response_model=SevaSutraFeedResponse)
def sevasutra_projects(
    principal: Annotated[ServiceIntegrationPrincipal, Depends(require_audit_service_principal)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> SevaSutraFeedResponse:
    if principal.subject != _SEVASUTRA_SERVICE_SUBJECT:
        raise AuditCoreError(
            error_code="VAC-SEVA-001",
            status_code=403,
            title="Not permitted",
            detail="This service identity may not read the SevaSutra Project feed.",
        )
    projects = load_service_projects(engine)
    logger.info("sevasutra_feed_read", projects=len(projects))
    return SevaSutraFeedResponse(generatedAt=datetime.now().astimezone(), projects=projects)
