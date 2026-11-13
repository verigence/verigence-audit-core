"""SevaSutra instances for service-audit Projects (product owner, 2026-11-09).

A Super Admin creates a Project's SevaSutra instance in Project Administration: the dealership's name and
its address (``<subdomain>.sevasutra.jbrconsulting.in``). SevaSutra reads the request from its Project feed
(``sevasutra_feed``), creates the dealership, and its deployment workflow (a GitHub Action) puts the
address live; SevaSutra reports back what it holds through the service endpoint below, which this module
keeps for Project Administration to show.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
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
    require_super_admin_request,
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
    # Attach this existing SevaSutra dealership (one no Project owns, e.g. the demo) instead of a new one.
    existingDealerCode: str | None = Field(default=None, max_length=60)


class SevaSutraFreeDealership(BaseModel):
    dealerCode: str
    dealershipName: str
    outlets: int
    visits: int
    reportedAtUtc: datetime


class SevaSutraFreeDealerships(BaseModel):
    dealerships: list[SevaSutraFreeDealership]


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
    attach = (body.existingDealerCode or "").strip().lower() or None
    if attach is not None:
        if current is not None and current.status != "REQUESTED":
            raise BusinessValidationError(detail="This Project already has its dealership in SevaSutra.")
        if not connection.execute(
            text("SELECT 1 FROM auditcore.sevasutra_free_dealerships WHERE dealer_code = :c"), {"c": attach}
        ).first():
            raise BusinessValidationError(detail="That SevaSutra dealership is not free to attach. Refresh the list and choose again.")
        changed_address = True
    try:
        with connection.begin_nested():
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.sevasutra_instances
                        (tenant_id, dealership_name, subdomain, status, dealer_code, requested_by_actor_id, requested_at_utc, updated_at_utc)
                    VALUES (:tenant_id, :name, :subdomain, 'REQUESTED', :attach, :actor, now(), now())
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        dealership_name = EXCLUDED.dealership_name,
                        subdomain = EXCLUDED.subdomain,
                        dealer_code = CASE WHEN CAST(:attach AS varchar) IS NOT NULL THEN CAST(:attach AS varchar) ELSE auditcore.sevasutra_instances.dealer_code END,
                        status = CASE WHEN :changed THEN 'REQUESTED' ELSE auditcore.sevasutra_instances.status END,
                        status_message = CASE WHEN :changed THEN NULL ELSE auditcore.sevasutra_instances.status_message END,
                        requested_by_actor_id = EXCLUDED.requested_by_actor_id,
                        updated_at_utc = now()
                    """
                ),
                {"tenant_id": tenant_id, "name": name, "subdomain": subdomain, "attach": attach, "actor": admin_request.user_id, "changed": changed_address},
            )
    except IntegrityError as exc:
        if "ux_sevasutra_instances_subdomain" in str(exc.orig):
            raise ConflictError(error_code="VAC-SEVA-003", title="Address in use", detail=f"{subdomain}.sevasutra.jbrconsulting.in is already used by another Project.") from exc
        raise
    # A new or changed instance is read by SevaSutra within a minute, not at tomorrow's daily sync.
    _ask_for_sync(admin_request.user_id)
    logger.info("sevasutra_instance_requested", tenant_id=tenant_id, subdomain=subdomain, attach=attach)
    return SevaSutraInstanceEnvelope(instance=_read(connection, tenant_id))


@router.get("/v1/sevasutra/free-dealerships", response_model=SevaSutraFreeDealerships)
def free_dealerships(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> SevaSutraFreeDealerships:
    """SevaSutra dealerships no Project owns yet (the demo, for one), as SevaSutra last reported them."""
    rows = connection.execute(
        text(
            "SELECT dealer_code, dealership_name, outlets, visits, reported_at_utc"
            " FROM auditcore.sevasutra_free_dealerships ORDER BY dealership_name"
        )
    ).mappings().all()
    return SevaSutraFreeDealerships(
        dealerships=[
            SevaSutraFreeDealership(
                dealerCode=r["dealer_code"], dealershipName=r["dealership_name"], outlets=r["outlets"],
                visits=r["visits"], reportedAtUtc=r["reported_at_utc"],
            )
            for r in rows
        ]
    )


# ---------------------------------------------------------------- what SevaSutra reports back


class SevaSutraInstanceReport(BaseModel):
    tenantId: str
    dealershipName: str
    dealerCode: str
    # The address live in SevaSutra now, if any; and whether it is the one asked for.
    liveHostname: str | None = None
    status: str = Field(pattern=r"^(CREATED|LIVE|FAILED)$")
    statusMessage: str | None = Field(default=None, max_length=500)


class SevaSutraFreeReport(BaseModel):
    dealerCode: str = Field(min_length=1, max_length=60)
    dealershipName: str = Field(min_length=1, max_length=240)
    outlets: int = 0
    visits: int = 0


class SevaSutraReportBatch(BaseModel):
    instances: list[SevaSutraInstanceReport]
    # The dealerships no Project owns; replaces the last list. Absent: the list is left as it is.
    unlinked: list[SevaSutraFreeReport] | None = None


# ── "Sync with SevaSutra" (product owner 2026-11-13) ────────────────────────
# "SevaSutra doesnt need to sync every 5 minutes, it can be once in a day, or whenever change
# happens lets have a sync button on project admin to sync data with sevasutra."
#
# SevaSutra read the Project feed every five minutes, day and night, so the database never
# stopped. It now reads it once a day, at sign-in, and when a Super Admin presses Sync in Project
# Administration. Pressing Sync only notes the time here; SevaSutra asks for that time once a
# minute and reads the feed when it is newer than its last read. Asking costs nothing: the time
# is kept in this process, no database is touched, so the night's sleep is not broken
# (audit_core.sleep_window). SevaSutra refuses requests from other sites, and it calls Audit Core,
# never the other way round; this keeps both as they are.
#
# Kept in memory on purpose: the API runs as one process, and a restart in the minute between a
# press and SevaSutra's next question loses only that press (the person presses again).
_sync_request: dict[str, str | None] = {"requestedAtUtc": None, "requestedBy": None}


def _ask_for_sync(requested_by: str | None) -> None:
    _sync_request.update(requestedAtUtc=datetime.now(UTC).isoformat(), requestedBy=requested_by)
    logger.info("sevasutra_sync_requested", requested_by=requested_by)


class SevaSutraSyncRequest(BaseModel):
    requestedAtUtc: str | None
    requestedBy: str | None = None


@router.post("/v1/sevasutra/sync-request", response_model=SevaSutraSyncRequest)
def request_sync(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
) -> SevaSutraSyncRequest:
    """A Super Admin asks SevaSutra to read Project Administration now (within a minute)."""
    _ask_for_sync(admin_request.user_id)
    return SevaSutraSyncRequest(**_sync_request)


@router.get("/v1/service/sevasutra/sync-request", response_model=SevaSutraSyncRequest)
def sync_request(
    principal: Annotated[ServiceIntegrationPrincipal, Depends(require_audit_service_principal)],
) -> SevaSutraSyncRequest:
    """When a sync was last asked for. SevaSutra's own question; no database is read."""
    if principal.subject != _SEVASUTRA_SERVICE_SUBJECT:
        raise AuditCoreError(error_code="VAC-SEVA-001", status_code=403, title="Not permitted", detail="This service identity may not read SevaSutra sync requests.")
    return SevaSutraSyncRequest(requestedAtUtc=_sync_request["requestedAtUtc"])


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
    if body.unlinked is not None:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM auditcore.sevasutra_free_dealerships"))
            for f in body.unlinked:
                connection.execute(
                    text(
                        "INSERT INTO auditcore.sevasutra_free_dealerships (dealer_code, dealership_name, outlets, visits)"
                        " VALUES (:c, :n, :o, :v) ON CONFLICT (dealer_code) DO NOTHING"
                    ),
                    {"c": f.dealerCode.lower(), "n": f.dealershipName, "o": f.outlets, "v": f.visits},
                )
    logger.info("sevasutra_instances_reported", count=updated, unlinked=None if body.unlinked is None else len(body.unlinked))
    return {"updated": updated}
