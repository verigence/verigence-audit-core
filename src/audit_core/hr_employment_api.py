"""HR reports who has left (PUT /v1/service/hr/employment, 2026-11-09).

Only the HR service identity may call it. See people_employment for what Audit Core does with it.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine

from audit_core.dependencies import get_engine
from audit_core.errors import AuditCoreError
from audit_core.people_employment import end_leaver_roles, store_leavers
from audit_core.security import ServiceIntegrationPrincipal
from audit_core.uc03_pc_booking_documents import require_audit_service_principal

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["HR employment"])

_HR_SERVICE_SUBJECT = "hrmgmt"
_MAX_PEOPLE = 5000


class Leaver(BaseModel):
    model_config = ConfigDict(extra="ignore")
    securityUserId: str = Field(min_length=1, max_length=128)
    employeeCode: str | None = Field(default=None, max_length=40)
    status: str = Field(min_length=1, max_length=20)
    exitDate: date | None = None


class EmploymentReport(BaseModel):
    leavers: list[Leaver] = Field(max_length=_MAX_PEOPLE)


class EmploymentReportResult(BaseModel):
    receivedAt: datetime
    leavers: int
    rolesEnded: int


@router.put("/v1/service/hr/employment", response_model=EmploymentReportResult)
def hr_employment(
    body: EmploymentReport,
    principal: Annotated[ServiceIntegrationPrincipal, Depends(require_audit_service_principal)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> EmploymentReportResult:
    if principal.subject != _HR_SERVICE_SUBJECT:
        raise AuditCoreError(
            error_code="VAC-HR-002",
            status_code=403,
            title="Not permitted",
            detail="This service identity may not report employment.",
        )
    leavers = [
        {"actorId": p.securityUserId.strip(), "employeeCode": p.employeeCode,
         "status": p.status.strip().upper(), "exitDate": p.exitDate}
        for p in body.leavers
        if p.status.strip().upper() != "ACTIVE" and p.securityUserId.strip()
    ]
    stored = store_leavers(engine, leavers)
    ended = end_leaver_roles(engine)
    logger.info("hr_employment_reported", leavers=stored, roles_ended=ended)
    return EmploymentReportResult(receivedAt=datetime.now().astimezone(), leavers=stored, rolesEnded=ended)
