"""Read-only work context for the HR service (HRMgmt).

HR needs to know, for each person, which projects they work on, in which role, and at which
outlets (with coordinates) so it can judge a PC's attendance against their assigned outlets and
route approvals to the right Team Lead or Project Manager. This endpoint only reads. It writes
nothing, adds no tables and touches no existing code path. HR calls it once a day, never while a
person is waiting, with a short timeout and no retry.
"""

from __future__ import annotations

from datetime import datetime
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
router = APIRouter(tags=["HR work context"])

# Only the HR service identity may read this.
_HR_SERVICE_SUBJECT = "hrmgmt"
_STATEMENT_TIMEOUT = "5s"


class WorkAssignment(BaseModel):
    securityUserId: str
    tenantId: str
    projectCode: str
    projectName: str
    roleCode: str
    dealerId: UUID | None
    dealerName: str | None
    outletId: UUID | None
    outletCode: str | None
    outletName: str | None
    latitude: float | None
    longitude: float | None
    effectiveFrom: datetime
    effectiveTo: datetime | None


class WorkContextResponse(BaseModel):
    generatedAt: datetime
    assignments: list[WorkAssignment]


_PROJECTS_SQL = text(
    """
    SELECT tenant_id, project_code, project_name
    FROM auditcore.projects
    WHERE project_status = 'ACTIVE'
    ORDER BY tenant_id
    """
)

_ASSIGNMENTS_SQL = text(
    """
    SELECT a.security_actor_id, a.business_role_code, a.dealer_id, a.outlet_id,
           a.effective_from, a.effective_to,
           d.dealer_name, o.outlet_code, o.outlet_name, o.latitude, o.longitude
    FROM auditcore.business_assignments a
    LEFT JOIN auditcore.dealers d
           ON d.tenant_id = a.tenant_id AND d.dealer_id = a.dealer_id
    LEFT JOIN auditcore.dealer_outlets o
           ON o.tenant_id = a.tenant_id AND o.dealer_id = a.dealer_id AND o.outlet_id = a.outlet_id
    WHERE a.tenant_id = :tenant_id
      AND a.assignment_status = 'ACTIVE'
      AND a.effective_from <= now()
      AND (a.effective_to IS NULL OR a.effective_to > now())
    ORDER BY a.security_actor_id, a.business_role_code, o.outlet_code
    """
)


def load_work_context(engine: Engine) -> list[dict[str, Any]]:
    """Every active project assignment with its outlet coordinates, one query per project."""
    rows: list[dict[str, Any]] = []
    with engine.begin() as connection:
        connection.execute(
            text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'")
        )
        set_platform_super_admin_context(connection)
        projects = connection.execute(_PROJECTS_SQL).mappings().all()
    for project in projects:
        with engine.begin() as connection:
            connection.execute(
                text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'")
            )
            set_tenant_context(connection, project["tenant_id"])
            for row in connection.execute(
                _ASSIGNMENTS_SQL, {"tenant_id": project["tenant_id"]}
            ).mappings():
                rows.append({**dict(row), **dict(project)})
    return rows


@router.get("/v1/service/hr/work-context", response_model=WorkContextResponse)
def hr_work_context(
    principal: Annotated[
        ServiceIntegrationPrincipal, Depends(require_audit_service_principal)
    ],
    engine: Annotated[Engine, Depends(get_engine)],
) -> WorkContextResponse:
    if principal.subject != _HR_SERVICE_SUBJECT:
        raise AuditCoreError(
            error_code="VAC-HR-001",
            status_code=403,
            title="Not permitted",
            detail="This service identity may not read the HR work context.",
        )
    rows = load_work_context(engine)
    assignments = [
        WorkAssignment(
            securityUserId=str(r["security_actor_id"]),
            tenantId=str(r["tenant_id"]),
            projectCode=str(r["project_code"]),
            projectName=str(r["project_name"]),
            roleCode=str(r["business_role_code"]),
            dealerId=r["dealer_id"],
            dealerName=r["dealer_name"],
            outletId=r["outlet_id"],
            outletCode=r["outlet_code"],
            outletName=r["outlet_name"],
            latitude=float(r["latitude"]) if r["latitude"] is not None else None,
            longitude=float(r["longitude"]) if r["longitude"] is not None else None,
            effectiveFrom=r["effective_from"],
            effectiveTo=r["effective_to"],
        )
        for r in rows
    ]
    logger.info("hr_work_context_read", assignments=len(assignments))
    return WorkContextResponse(
        generatedAt=datetime.now().astimezone(), assignments=assignments
    )
