from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.errors import NotFoundError
from audit_core.security_integration import SecurityAdminClient, SecurityAdminError

router = APIRouter(prefix="/v1/tenants/{tenant_id}/project", tags=["project-readiness"])

ReadinessSeverity = Literal["BLOCKING", "WARNING", "INFO"]
ReadinessStatus = Literal["PASS", "FAIL", "PENDING"]


class ReadinessCheck(BaseModel):
    area: str
    checkKey: str
    severity: ReadinessSeverity
    status: ReadinessStatus
    message: str
    targetTask: str


class ProjectReadinessResponse(BaseModel):
    readyToActivate: bool
    evaluatedAtUtc: datetime
    checks: list[ReadinessCheck]


def _security_base_url() -> str:
    value = os.environ.get("SECURITY_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("SECURITY_BASE_URL is required for UC02 administration")
    return value


def _project_state(connection: Connection, tenant_id: str):
    row = connection.execute(
        text(
            """
            SELECT project_name, oem_id, product_category_id, effective_start_date,
                   timezone_name, project_status
            FROM auditcore.projects
            WHERE tenant_id = :tenant_id
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-001",
            title="Project not found",
            detail="Project not found for the requested tenant.",
        )
    return row


def _project_setup_check(project) -> ReadinessCheck:
    complete = bool(
        str(project["project_name"]).strip()
        and project["oem_id"] is not None
        and project["product_category_id"] is not None
        and project["effective_start_date"] is not None
        and str(project["timezone_name"]).strip()
        and project["project_status"] in {"CONFIGURING", "ACTIVE"}
    )
    return ReadinessCheck(
        area="PROJECT",
        checkKey="PROJECT_SETUP_COMPLETE",
        severity="BLOCKING",
        status="PASS" if complete else "FAIL",
        message=(
            "Project setup is complete."
            if complete
            else "Complete the required Project Details before activation."
        ),
        targetTask="PROJECT_DETAILS",
    )


def _security_tenant_check(
    *,
    tenant_id: str,
    human_bearer_token: str,
) -> ReadinessCheck:
    try:
        with SecurityAdminClient(base_url=_security_base_url()) as client:
            tenant = client.get_tenant(
                human_bearer_token=human_bearer_token,
                tenant_id=tenant_id,
            )
    except SecurityAdminError as exc:
        if exc.http_status == 404:
            return ReadinessCheck(
                area="SECURITY",
                checkKey="SECURITY_TENANT_LIFECYCLE",
                severity="BLOCKING",
                status="FAIL",
                message="Security Tenant is missing for this Project.",
                targetTask="PROJECT_DETAILS",
            )
        return ReadinessCheck(
            area="SECURITY",
            checkKey="SECURITY_TENANT_LIFECYCLE",
            severity="BLOCKING",
            status="PENDING",
            message="Security Tenant readiness could not be verified yet.",
            targetTask="PROJECT_DETAILS",
        )

    valid = tenant.status in {"CONFIGURING", "ACTIVE"}
    return ReadinessCheck(
        area="SECURITY",
        checkKey="SECURITY_TENANT_LIFECYCLE",
        severity="BLOCKING",
        status="PASS" if valid else "FAIL",
        message=(
            f"Security Tenant is {tenant.status}."
            if valid
            else f"Security Tenant lifecycle {tenant.status} is not activation-ready."
        ),
        targetTask="PROJECT_DETAILS",
    )


def _dealer_outlet_structure_check(
    connection: Connection,
    tenant_id: str,
) -> ReadinessCheck:
    counts = connection.execute(
        text(
            """
            SELECT
                (SELECT count(*) FROM auditcore.dealers
                 WHERE tenant_id = :tenant_id AND status = 'ACTIVE') AS active_dealers,
                (SELECT count(*) FROM auditcore.dealer_outlets
                 WHERE tenant_id = :tenant_id AND status = 'ACTIVE') AS active_outlets
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one()
    valid = counts["active_dealers"] > 0 and counts["active_outlets"] > 0
    return ReadinessCheck(
        area="PROJECT_STRUCTURE",
        checkKey="DEALER_OUTLET_STRUCTURE",
        severity="BLOCKING",
        status="PASS" if valid else "FAIL",
        message=(
            "Active Dealer and Dealer Outlet structure is present."
            if valid
            else "Add at least one active Dealer and active Dealer Outlet before activation."
        ),
        targetTask="DEALER_OUTLETS" if counts["active_dealers"] else "DEALERS",
    )


def _pc_coverage_check(connection: Connection, tenant_id: str) -> ReadinessCheck:
    uncovered = connection.execute(
        text(
            """
            SELECT o.outlet_id
            FROM auditcore.dealer_outlets o
            WHERE o.tenant_id = :tenant_id
              AND o.status = 'ACTIVE'
              AND NOT EXISTS (
                  SELECT 1
                  FROM auditcore.business_assignments a
                  WHERE a.tenant_id = o.tenant_id
                    AND a.outlet_id = o.outlet_id
                    AND a.business_role_code = 'PC'
                    AND a.assignment_status = 'ACTIVE'
                    AND a.effective_from <= now()
                    AND (a.effective_to IS NULL OR a.effective_to > now())
              )
            ORDER BY o.outlet_id
            """
        ),
        {"tenant_id": tenant_id},
    ).scalars().all()
    covered = not uncovered
    return ReadinessCheck(
        area="ROLE_MAPPING",
        checkKey="ACTIVE_OUTLET_PC_COVERAGE",
        severity="BLOCKING",
        status="PASS" if covered else "FAIL",
        message=(
            "Every active Dealer Outlet has an active PC mapping."
            if covered
            else f"{len(uncovered)} active Dealer Outlet(s) still require an active PC mapping."
        ),
        targetTask="ROLE_MAPPING",
    )


def _optional_map_metadata_check(connection: Connection, tenant_id: str) -> ReadinessCheck:
    incomplete = connection.execute(
        text(
            """
            SELECT count(*)
            FROM auditcore.dealer_outlets
            WHERE tenant_id = :tenant_id
              AND status = 'ACTIVE'
              AND (
                  google_place_id IS NULL
                  OR btrim(google_place_id) = ''
                  OR latitude IS NULL
                  OR longitude IS NULL
              )
            """
        ),
        {"tenant_id": tenant_id},
    ).scalar_one()
    return ReadinessCheck(
        area="OUTLET_LOCATION",
        checkKey="OPTIONAL_OUTLET_MAP_METADATA",
        severity="WARNING",
        status="PASS" if incomplete == 0 else "FAIL",
        message=(
            "Optional Google Place and coordinate metadata is complete."
            if incomplete == 0
            else (
                f"{incomplete} active Dealer Outlet(s) use manual or incomplete map metadata; "
                "this does not block activation."
            )
        ),
        targetTask="DEALER_OUTLETS",
    )


def _pending_owned_dependency_checks() -> list[ReadinessCheck]:
    return [
        ReadinessCheck(
            area="PROJECT_MASTERS",
            checkKey="PROJECT_MASTERS_READY",
            severity="BLOCKING",
            status="PENDING",
            message="Project Master readiness will be evaluated by the Project Masters package.",
            targetTask="PROJECT_MASTERS",
        ),
        ReadinessCheck(
            area="DI",
            checkKey="DI_PROJECT_READY",
            severity="BLOCKING",
            status="PENDING",
            message="Document Intelligence readiness will be evaluated by the DI package.",
            targetTask="PROJECT_MASTERS",
        ),
    ]


def evaluate_project_readiness(
    *,
    tenant_id: str,
    admin_request: HumanAdminRequest,
    connection: Connection,
) -> ProjectReadinessResponse:
    set_tenant_context(connection, tenant_id)
    project = _project_state(connection, tenant_id)
    checks = [
        _project_setup_check(project),
        _security_tenant_check(
            tenant_id=tenant_id,
            human_bearer_token=admin_request.bearer_token,
        ),
        _dealer_outlet_structure_check(connection, tenant_id),
        _pc_coverage_check(connection, tenant_id),
        *_pending_owned_dependency_checks(),
        _optional_map_metadata_check(connection, tenant_id),
    ]
    ready = all(
        check.status == "PASS"
        for check in checks
        if check.severity == "BLOCKING"
    )
    return ProjectReadinessResponse(
        readyToActivate=ready,
        evaluatedAtUtc=datetime.now(UTC),
        checks=checks,
    )


@router.get("/readiness", response_model=ProjectReadinessResponse)
def get_project_readiness(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectReadinessResponse:
    return evaluate_project_readiness(
        tenant_id=tenant_id,
        admin_request=admin_request,
        connection=connection,
    )
