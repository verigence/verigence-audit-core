"""SevaSutra instances for service-audit Projects (product owner, 2026-11-09).

A Super Admin creates a Project's SevaSutra instance in Project Administration: the dealership's name and
its address (``<subdomain>.sevasutra.jbrconsulting.in``). SevaSutra reads the request from its Project feed
(``sevasutra_feed``), creates the dealership, and its deployment workflow (a GitHub Action) puts the
address live; SevaSutra reports back what it holds through the service endpoint below, which this module
keeps for Project Administration to show.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_engine,
    require_project_admin_request,
)
from audit_core.errors import (
    AuditCoreError,
    BusinessValidationError,
    ConflictError,
    NotFoundError,
)
from audit_core.security import ServiceIntegrationPrincipal
from audit_core.uc03_pc_booking_documents import require_audit_service_principal

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["SevaSutra instances"])

_SEVASUTRA_SERVICE_SUBJECT = "sevasutra"
_SUBDOMAIN = r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$"
# Addresses SevaSutra itself uses, never given to an instance.
_RESERVED = frozenset({"www", "api", "app", "admin", "platform", "dev", "staging", "sevasutra"})


class SevaSutraInstance(BaseModel):
    tenantId: str
    dealershipName: str
    subdomain: str | None
    status: str
    statusMessage: str | None
    dealerCode: str | None
    liveHostname: str | None
    requestedAtUtc: datetime
    reportedAtUtc: datetime | None


class SevaSutraInstanceRequest(BaseModel):
    dealershipName: str = Field(min_length=2, max_length=240)
    # The part before .sevasutra.jbrconsulting.in; empty for no address of its own.
    subdomain: str | None = Field(default=None, max_length=40)


class SevaSutraInstanceEnvelope(BaseModel):
    instance: SevaSutraInstance | None


_SELECT = """
    SELECT tenant_id, dealership_name, subdomain, status, status_message, dealer_code, live_hostname,
           requested_at_utc, reported_at_utc
    FROM auditcore.sevasutra_instances WHERE tenant_id = :tenant_id
"""


def _instance(row) -> SevaSutraInstance:
    return SevaSutraInstance(
        tenantId=str(row["tenant_id"]),
        dealershipName=str(row["dealership_name"]),
        subdomain=row["subdomain"],
        status=str(row["status"]),
        statusMessage=row["status_message"],
        dealerCode=row["dealer_code"],
        liveHostname=row["live_hostname"],
        requestedAtUtc=row["requested_at_utc"],
        reportedAtUtc=row["reported_at_utc"],
    )


def _read(connection: Connection, tenant_id: str) -> SevaSutraInstance | None:
    row = connection.execute(text(_SELECT), {"tenant_id": tenant_id}).mappings().one_or_none()
    return _instance(row) if row else None


@router.get("/v1/tenants/{tenant_id}/sevasutra-instance", response_model=SevaSutraInstanceEnvelope)
def get_instance(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> SevaSutraInstanceEnvelope:
    set_tenant_context(connection, tenant_id)
    return SevaSutraInstanceEnvelope(instance=_read(connection, tenant_id))


@router.put("/v1/tenants/{tenant_id}/sevasutra-instance", response_model=SevaSutraInstanceEnvelope)
def put_instance(
    tenant_id: str,
    body: SevaSutraInstanceRequest,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> SevaSutraInstanceEnvelope:
    """Creates the Project's SevaSutra instance, or changes its name or address. Super Admin only."""
    if not admin_request.admin_context.is_super_admin:
        raise AuditCoreError(error_code="VAC-SEVA-002", status_code=403, title="Not permitted", detail="Only a Super Admin can create a SevaSutra instance.")
    set_tenant_context(connection, tenant_id)
    project = connection.execute(
        text("SELECT audit_type, project_status FROM auditcore.projects WHERE tenant_id = :tenant_id"), {"tenant_id": tenant_id}
    ).mappings().one_or_none()
    if project is None:
        raise NotFoundError(error_code="VAC-NF-001", title="Project not found", detail="Project not found for the requested tenant.")
    if project["audit_type"] != "SERVICE":
        raise BusinessValidationError(detail="A SevaSutra instance is for a service-audit Project. Set the Project's audit type to Service audit first.")
    subdomain = (body.subdomain or "").strip().lower() or None
    if subdomain is not None and (not re.match(_SUBDOMAIN, subdomain) or subdomain in _RESERVED):
        raise BusinessValidationError(detail="The address may use letters, digits and hyphens (2-40), and cannot be a reserved word.")
    current = _read(connection, tenant_id)
    name = body.dealershipName.strip()
    changed_address = current is None or current.subdomain != subdomain
    try:
        with connection.begin_nested():
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.sevasutra_instances
                        (tenant_id, dealership_name, subdomain, status, requested_by_actor_id, requested_at_utc, updated_at_utc)
                    VALUES (:tenant_id, :name, :subdomain, 'REQUESTED', :actor, now(), now())
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        dealership_name = EXCLUDED.dealership_name,
                        subdomain = EXCLUDED.subdomain,
                        status = CASE WHEN :changed THEN 'REQUESTED' ELSE auditcore.sevasutra_instances.status END,
                        status_message = CASE WHEN :changed THEN NULL ELSE auditcore.sevasutra_instances.status_message END,
                        requested_by_actor_id = EXCLUDED.requested_by_actor_id,
                        updated_at_utc = now()
                    """
                ),
                {"tenant_id": tenant_id, "name": name, "subdomain": subdomain, "actor": admin_request.user_id, "changed": changed_address},
            )
    except IntegrityError as exc:
        if "ux_sevasutra_instances_subdomain" in str(exc.orig):
            raise ConflictError(error_code="VAC-SEVA-003", title="Address in use", detail=f"{subdomain}.sevasutra.jbrconsulting.in is already used by another Project.") from exc
        raise
    logger.info("sevasutra_instance_requested", tenant_id=tenant_id, subdomain=subdomain)
    return SevaSutraInstanceEnvelope(instance=_read(connection, tenant_id))


# ---------------------------------------------------------------- what SevaSutra reports back


class SevaSutraInstanceReport(BaseModel):
    tenantId: str
    dealershipName: str
    dealerCode: str
    # The address live in SevaSutra now, if any; and whether it is the one asked for.
    liveHostname: str | None = None
    status: str = Field(pattern=r"^(CREATED|LIVE|FAILED)$")
    statusMessage: str | None = Field(default=None, max_length=500)


class SevaSutraReportBatch(BaseModel):
    instances: list[SevaSutraInstanceReport]


@router.post("/v1/service/sevasutra/instances")
def report_instances(
    body: SevaSutraReportBatch,
    principal: Annotated[ServiceIntegrationPrincipal, Depends(require_audit_service_principal)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> dict[str, int]:
    """SevaSutra's account of the dealership each Project has there. A dealership linked before instances
    were requested here (Shree, for one) is recorded as its instance."""
    if principal.subject != _SEVASUTRA_SERVICE_SUBJECT:
        raise AuditCoreError(error_code="VAC-SEVA-001", status_code=403, title="Not permitted", detail="This service identity may not report SevaSutra instances.")
    updated = 0
    for r in body.instances:
        with engine.begin() as connection:
            set_tenant_context(connection, r.tenantId)
            if not connection.execute(text("SELECT 1 FROM auditcore.projects WHERE tenant_id = :t"), {"t": r.tenantId}).first():
                continue
            subdomain = None
            if r.liveHostname and r.liveHostname.endswith(".sevasutra.jbrconsulting.in"):
                subdomain = r.liveHostname.split(".", 1)[0]
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.sevasutra_instances
                        (tenant_id, dealership_name, subdomain, status, status_message, dealer_code, live_hostname, reported_at_utc, updated_at_utc)
                    VALUES (:t, :name, :subdomain, :status, :message, :code, :live, now(), now())
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        status_message = EXCLUDED.status_message,
                        dealer_code = EXCLUDED.dealer_code,
                        live_hostname = EXCLUDED.live_hostname,
                        reported_at_utc = now(),
                        updated_at_utc = now()
                    """
                ),
                {"t": r.tenantId, "name": r.dealershipName, "subdomain": subdomain, "status": r.status, "message": r.statusMessage, "code": r.dealerCode, "live": r.liveHostname},
            )
            updated += 1
    logger.info("sevasutra_instances_reported", count=updated)
    return {"updated": updated}
