from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import HumanAdminRequest, get_connection, require_project_admin_request
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import NotFoundError
from audit_core.security_integration import SecurityAdminClient, SecurityAdminError

router = APIRouter(prefix="/v1/tenants/{tenant_id}/project", tags=["project-readiness"])

ReadinessSeverity = Literal["BLOCKING", "WARNING", "INFO"]
ReadinessStatus = Literal["PASS", "FAIL", "PENDING"]

_REQUIRED_AUDIT_CORE_MASTERS = {
    "PROJECT_POLICY": "Project Policy",
    "DOCUMENT_REQUIREMENT_PROFILE": "Document Requirement Profile",
    "AUDIT_CONTROL": "Audit Control",
}
# Audit Core owns Journey evidence requirements. DI Requirement Profiles remain
# an optional advanced DI capability and are intentionally not an activation gate.
_REQUIRED_DI_MASTER_STATES = {
    "DOCUMENT_TYPES": "ACTIVE",
    "EXTRACTION_PROFILES": "PUBLISHED",
}
_DI_MASTER_LABELS = {
    "DOCUMENT_TYPES": "Document Types",
    "EXTRACTION_PROFILES": "Extraction Profiles",
}


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


def _di_base_url() -> str:
    value = os.environ.get("DI_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("DI_BASE_URL is required for UC02 administration")
    return value


def _project_state(connection: Connection, tenant_id: str):
    row = connection.execute(
        text(
            """
            SELECT p.project_name, p.oem_id, p.product_category_id, o.oem_code,
                   p.effective_start_date, p.timezone_name, p.project_status,
                   (SELECT count(*) FROM auditcore.segments s WHERE s.is_active=true)
                     AS configured_segments,
                   (SELECT count(*) FROM auditcore.project_segments ps
                    WHERE ps.tenant_id=p.tenant_id) AS selected_segments
            FROM auditcore.projects p
            JOIN auditcore.oems o ON o.oem_id=p.oem_id
            WHERE p.tenant_id=:tenant_id
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
    segment_selection_complete = (
        project["product_category_id"] is not None
        or int(project["configured_segments"]) == 0
        or int(project["selected_segments"]) > 0
    )
    complete = bool(
        str(project["project_name"]).strip()
        and project["oem_id"] is not None
        and segment_selection_complete
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
            else "Complete the required Project Details and Segment selection before activation."
        ),
        targetTask="PROJECT_DETAILS",
    )


def _security_tenant_check(*, tenant_id: str, human_bearer_token: str) -> ReadinessCheck:
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
            message="Security Tenant readiness could not be verified yet. Refresh the check before activation.",
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


def _dealer_outlet_structure_check(connection: Connection, tenant_id: str) -> ReadinessCheck:
    counts = connection.execute(
        text(
            """
            SELECT
                (SELECT count(*) FROM auditcore.dealers
                 WHERE tenant_id=:tenant_id AND status='ACTIVE') AS active_dealers,
                (SELECT count(*) FROM auditcore.dealer_outlets
                 WHERE tenant_id=:tenant_id AND status='ACTIVE') AS active_outlets
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one()
    valid = counts["active_dealers"] > 0 and counts["active_outlets"] > 0
    return ReadinessCheck(
        area="PROJECT_STRUCTURE",
        checkKey="DEALER_OUTLET_STRUCTURE",
        severity="WARNING",
        status="PASS" if valid else "FAIL",
        message=(
            "Active Dealer and Dealer Outlet structure is present."
            if valid
            else "Dealer/Outlet setup is incomplete. You can activate now and complete it before operational use."
        ),
        targetTask="DEALER_OUTLETS" if counts["active_dealers"] else "DEALERS",
    )


def _pc_coverage_check(connection: Connection, tenant_id: str) -> ReadinessCheck:
    uncovered = connection.execute(
        text(
            """
            SELECT o.outlet_id
            FROM auditcore.dealer_outlets o
            WHERE o.tenant_id=:tenant_id
              AND o.status='ACTIVE'
              AND NOT EXISTS (
                  SELECT 1
                  FROM auditcore.business_assignments a
                  WHERE a.tenant_id=o.tenant_id
                    AND a.outlet_id=o.outlet_id
                    AND a.business_role_code='PC'
                    AND a.assignment_status='ACTIVE'
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
        severity="WARNING",
        status="PASS" if covered else "FAIL",
        message=(
            "Every active Dealer Outlet has an active Process Consultant mapping."
            if covered
            else f"{len(uncovered)} active Dealer Outlet(s) still require an active Process Consultant mapping. This does not block activation."
        ),
        targetTask="ROLE_MAPPING",
    )


def _mahindra_master_gaps(connection: Connection, tenant_id: str) -> list[str]:
    segments = connection.execute(
        text(
            """
            SELECT s.segment_id, s.segment_name, s.segment_code
            FROM auditcore.project_segments ps
            JOIN auditcore.segments s ON s.segment_id=ps.segment_id
            WHERE ps.tenant_id=:tenant_id
            ORDER BY s.segment_name
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().all()
    missing: list[str] = []
    for segment in segments:
        product_ready = bool(
            connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM auditcore.project_product_masters m
                        JOIN auditcore.project_product_master_versions v
                          ON v.tenant_id=m.tenant_id
                         AND v.product_master_id=m.product_master_id
                        WHERE m.tenant_id=:tenant_id
                          AND m.segment_id=:segment_id
                          AND m.status='ACTIVE'
                          AND v.lifecycle_status='PUBLISHED'
                    )
                    """
                ),
                {"tenant_id": tenant_id, "segment_id": segment["segment_id"]},
            ).scalar_one()
        )
        if not product_ready:
            missing.append(f"{segment['segment_name']} Product Master")

        price_code = f"MAHINDRA_{segment['segment_code']}"[:120]
        price_ready = bool(
            connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM auditcore.price_lists pl
                        JOIN auditcore.price_list_versions v
                          ON v.tenant_id=pl.tenant_id
                         AND v.price_list_id=pl.price_list_id
                        WHERE pl.tenant_id=:tenant_id
                          AND pl.price_list_code=:price_code
                          AND v.lifecycle_status='PUBLISHED'
                    )
                    """
                ),
                {"tenant_id": tenant_id, "price_code": price_code},
            ).scalar_one()
        )
        if not price_ready:
            missing.append(f"{segment['segment_name']} Price Master")

    discount_ready = bool(
        connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM auditcore.discount_policy_versions
                    WHERE tenant_id=:tenant_id AND lifecycle_status='PUBLISHED'
                )
                """
            ),
            {"tenant_id": tenant_id},
        ).scalar_one()
    )
    if not discount_ready:
        missing.append("Discount & Policy Master")
    return missing


def _project_masters_check(connection: Connection, tenant_id: str) -> ReadinessCheck:
    oem_code = connection.execute(
        text(
            """
            SELECT o.oem_code
            FROM auditcore.projects p
            JOIN auditcore.oems o ON o.oem_id=p.oem_id
            WHERE p.tenant_id=:tenant_id
            """
        ),
        {"tenant_id": tenant_id},
    ).scalar_one()

    missing = _mahindra_master_gaps(connection, tenant_id) if oem_code == "MAHINDRA" else []
    if oem_code != "MAHINDRA":
        product_ready = bool(
            connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM auditcore.project_product_master_versions
                        WHERE tenant_id=:tenant_id AND lifecycle_status='PUBLISHED'
                    )
                    """
                ),
                {"tenant_id": tenant_id},
            ).scalar_one()
        )
        if not product_ready:
            missing.append("Product Master")

    queries = {
        "PROJECT_POLICY": """
            SELECT EXISTS (
                SELECT 1 FROM auditcore.project_policy_versions
                WHERE tenant_id=:tenant_id AND lifecycle_status='PUBLISHED'
            )
        """,
        "DOCUMENT_REQUIREMENT_PROFILE": """
            SELECT EXISTS (
                SELECT 1 FROM auditcore.document_requirement_profile_versions
                WHERE tenant_id=:tenant_id AND lifecycle_status='PUBLISHED'
            )
        """,
        "AUDIT_CONTROL": """
            SELECT EXISTS (
                SELECT 1 FROM auditcore.audit_control_versions
                WHERE tenant_id=:tenant_id AND lifecycle_status='PUBLISHED'
            )
        """,
    }
    missing.extend(
        _REQUIRED_AUDIT_CORE_MASTERS[key]
        for key, query in queries.items()
        if not bool(connection.execute(text(query), {"tenant_id": tenant_id}).scalar_one())
    )
    return ReadinessCheck(
        area="PROJECT_MASTERS",
        checkKey="PROJECT_MASTERS_READY",
        severity="WARNING",
        status="PASS" if not missing else "FAIL",
        message=(
            "Project business, policy, audit-control and document-requirement masters are published."
            if not missing
            else "Configuration still recommended: " + ", ".join(missing) + ". This does not block activation."
        ),
        targetTask="PROJECT_MASTERS",
    )


def _di_project_check(*, tenant_id: str, human_bearer_token: str) -> ReadinessCheck:
    try:
        with DiClient(base_url=_di_base_url()) as client:
            provisioning = client.get_project_provisioning(
                human_token=human_bearer_token,
                tenant_id=tenant_id,
            )
            if provisioning.get("provisioningStatus") != "READY":
                return ReadinessCheck(
                    area="DI",
                    checkKey="DI_PROJECT_READY",
                    severity="WARNING",
                    status="FAIL",
                    message="Document Intelligence provisioning is incomplete. This does not block Project activation.",
                    targetTask="DOCUMENT_INTELLIGENCE",
                )

            catalogue = client.list_project_masters(
                human_token=human_bearer_token,
                tenant_id=tenant_id,
            )
            available = {
                str(item.get("masterKey")): item
                for item in catalogue
                if isinstance(item.get("masterKey"), str)
            }
            missing_domains = [key for key in _REQUIRED_DI_MASTER_STATES if key not in available]
            wrong_states: list[str] = []
            uses_verigence_default = False
            for master_key, expected_state in _REQUIRED_DI_MASTER_STATES.items():
                if master_key not in available:
                    continue
                payload = client.list_project_master_versions(
                    human_token=human_bearer_token,
                    tenant_id=tenant_id,
                    master_key=master_key,
                )
                versions = payload.get("versions")
                if not isinstance(versions, list):
                    wrong_states.append(master_key)
                    continue
                matching = [
                    version
                    for version in versions
                    if isinstance(version, dict) and version.get("status") == expected_state
                ]
                if not matching:
                    wrong_states.append(master_key)
                if any(
                    version.get("configurationSource") == "VERIGENCE_DEFAULT"
                    or version.get("inherited") is True
                    for version in matching
                ):
                    uses_verigence_default = True
    except DiClientError:
        return ReadinessCheck(
            area="DI",
            checkKey="DI_PROJECT_READY",
            severity="WARNING",
            status="PENDING",
            message="Document Intelligence readiness could not be verified. This does not block Project activation.",
            targetTask="DOCUMENT_INTELLIGENCE",
        )

    failures = [*missing_domains, *wrong_states]
    if failures:
        actions = [
            f"{_DI_MASTER_LABELS.get(key, key)} must expose an effective {_REQUIRED_DI_MASTER_STATES.get(key, 'configured')} version"
            for key in failures
        ]
        message = "Review Document Intelligence configuration: " + "; ".join(actions) + ". This does not block activation."
    elif uses_verigence_default:
        message = "Using Verigence default Document Intelligence configuration. Customize it if this Project requires different document types or extraction fields."
    else:
        message = "Document Intelligence has effective Document Types and Extraction Profiles. Requirement Profiles are optional."
    return ReadinessCheck(
        area="DI",
        checkKey="DI_PROJECT_READY",
        severity="WARNING",
        status="PASS" if not failures else "FAIL",
        message=message,
        targetTask="DOCUMENT_INTELLIGENCE",
    )


def _optional_map_metadata_check(connection: Connection, tenant_id: str) -> ReadinessCheck:
    incomplete = connection.execute(
        text(
            """
            SELECT count(*)
            FROM auditcore.dealer_outlets
            WHERE tenant_id=:tenant_id
              AND status='ACTIVE'
              AND (
                  google_place_id IS NULL
                  OR btrim(google_place_id)=''
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
            else f"{incomplete} active Dealer Outlet(s) use manual or incomplete map/GPS metadata; this does not block activation."
        ),
        targetTask="DEALER_OUTLETS",
    )


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
        _project_masters_check(connection, tenant_id),
        _di_project_check(
            tenant_id=tenant_id,
            human_bearer_token=admin_request.bearer_token,
        ),
        _optional_map_metadata_check(connection, tenant_id),
    ]
    ready = not any(
        check.severity == "BLOCKING" and check.status != "PASS"
        for check in checks
    )
    return ProjectReadinessResponse(
        readyToActivate=ready,
        evaluatedAtUtc=datetime.now(UTC),
        checks=checks,
    )


@router.get("/readiness", response_model=ProjectReadinessResponse)
def get_project_readiness(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectReadinessResponse:
    return evaluate_project_readiness(
        tenant_id=tenant_id,
        admin_request=admin_request,
        connection=connection,
    )
