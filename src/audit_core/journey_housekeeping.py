from __future__ import annotations

import os
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.errors import DependencyUnavailableError, NotFoundError

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/admin/housekeeping/journeys",
    tags=["journey-housekeeping"],
)

HousekeepingScope = Literal["TENANT", "OUTLET", "JOURNEY"]
_CONFIRMATION = "PURGE_JOURNEY_DATA"
_DI_BATCH_SIZE = 1000


class JourneyHousekeepingPreview(BaseModel):
    tenantId: str
    scope: HousekeepingScope
    scopeId: str
    journeys: int
    customers: int
    evidence: int
    diDocuments: int
    auditFindings: int
    payments: int
    deliveries: int
    workflowTasks: int


class JourneyHousekeepingCommand(BaseModel):
    scope: HousekeepingScope
    outletId: UUID | None = None
    journeyId: UUID | None = None
    confirmScopeId: str
    confirmation: Literal["PURGE_JOURNEY_DATA"]


class JourneyHousekeepingResult(BaseModel):
    tenantId: str
    scope: HousekeepingScope
    scopeId: str
    purgeStatus: Literal["REMOVED"]
    deletedJourneys: int
    deletedCustomers: int
    deletedEvidence: int
    deletedDiDocuments: int
    deletedDiStorageObjects: int
    masterDataPreserved: bool = True


def _di_base_url() -> str:
    value = os.environ.get("DI_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("DI_BASE_URL is required for Journey housekeeping")
    return value


def _scope_id(
    *,
    tenant_id: str,
    scope: HousekeepingScope,
    outlet_id: UUID | None,
    journey_id: UUID | None,
) -> str:
    if scope == "TENANT":
        if outlet_id is not None or journey_id is not None:
            raise HTTPException(
                status_code=400,
                detail="Tenant scope cannot include Outlet/Journey ID.",
            )
        return tenant_id
    if scope == "OUTLET":
        if outlet_id is None or journey_id is not None:
            raise HTTPException(
                status_code=400,
                detail="Outlet scope requires only outletId.",
            )
        return str(outlet_id)
    if journey_id is None or outlet_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Journey scope requires only journeyId.",
        )
    return str(journey_id)


def _target_journeys(
    connection: Connection,
    *,
    tenant_id: str,
    scope: HousekeepingScope,
    outlet_id: UUID | None,
    journey_id: UUID | None,
) -> list[UUID]:
    if scope == "TENANT":
        return list(
            connection.execute(
                text(
                    "SELECT journey_id FROM auditcore.journeys "
                    "WHERE tenant_id=:tenant_id ORDER BY created_at_utc, journey_id"
                ),
                {"tenant_id": tenant_id},
            ).scalars().all()
        )

    if scope == "OUTLET":
        assert outlet_id is not None
        outlet_exists = connection.execute(
            text(
                "SELECT 1 FROM auditcore.dealer_outlets "
                "WHERE tenant_id=:tenant_id AND outlet_id=:outlet_id"
            ),
            {"tenant_id": tenant_id, "outlet_id": outlet_id},
        ).scalar_one_or_none()
        if outlet_exists is None:
            raise NotFoundError(
                error_code="VAC-NF-003",
                title="Outlet not found",
                detail="Outlet not found for the selected Project/Tenant.",
            )
        return list(
            connection.execute(
                text(
                    "SELECT journey_id FROM auditcore.journeys "
                    "WHERE tenant_id=:tenant_id AND outlet_id=:outlet_id "
                    "ORDER BY created_at_utc, journey_id"
                ),
                {"tenant_id": tenant_id, "outlet_id": outlet_id},
            ).scalars().all()
        )

    assert journey_id is not None
    existing = connection.execute(
        text(
            "SELECT journey_id FROM auditcore.journeys "
            "WHERE tenant_id=:tenant_id AND journey_id=:journey_id"
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    if existing is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Journey not found",
            detail="Journey not found for the selected Project/Tenant.",
        )
    return [existing]


def _di_document_ids(
    connection: Connection,
    *,
    tenant_id: str,
    journey_ids: list[UUID],
) -> list[UUID]:
    if not journey_ids:
        return []
    return list(
        connection.execute(
            text(
                """
                SELECT DISTINCT di_document_id
                FROM (
                    SELECT di_document_id
                    FROM auditcore.evidence
                    WHERE tenant_id=:tenant_id
                      AND journey_id = ANY(CAST(:journey_ids AS uuid[]))
                      AND di_document_id IS NOT NULL
                    UNION
                    SELECT di_document_id
                    FROM auditcore.evidence_ingestion_operations
                    WHERE tenant_id=:tenant_id
                      AND journey_id = ANY(CAST(:journey_ids AS uuid[]))
                      AND di_document_id IS NOT NULL
                ) document_refs
                ORDER BY di_document_id
                """
            ),
            {"tenant_id": tenant_id, "journey_ids": journey_ids},
        ).scalars().all()
    )


def _preview(
    connection: Connection,
    *,
    tenant_id: str,
    scope: HousekeepingScope,
    scope_id: str,
    journey_ids: list[UUID],
) -> JourneyHousekeepingPreview:
    if not journey_ids:
        return JourneyHousekeepingPreview(
            tenantId=tenant_id,
            scope=scope,
            scopeId=scope_id,
            journeys=0,
            customers=0,
            evidence=0,
            diDocuments=0,
            auditFindings=0,
            payments=0,
            deliveries=0,
            workflowTasks=0,
        )
    params = {"tenant_id": tenant_id, "journey_ids": journey_ids}
    row = connection.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM auditcore.journeys
                 WHERE tenant_id=:tenant_id
                   AND journey_id = ANY(CAST(:journey_ids AS uuid[]))) AS journeys,
              (SELECT count(DISTINCT customer_id) FROM auditcore.journeys
                 WHERE tenant_id=:tenant_id
                   AND journey_id = ANY(CAST(:journey_ids AS uuid[]))) AS customers,
              (SELECT count(*) FROM auditcore.evidence
                 WHERE tenant_id=:tenant_id
                   AND journey_id = ANY(CAST(:journey_ids AS uuid[]))) AS evidence,
              (SELECT count(*) FROM auditcore.audit_findings
                 WHERE tenant_id=:tenant_id
                   AND journey_id = ANY(CAST(:journey_ids AS uuid[]))) AS findings,
              (SELECT count(*) FROM auditcore.payments
                 WHERE tenant_id=:tenant_id
                   AND journey_id = ANY(CAST(:journey_ids AS uuid[]))) AS payments,
              (SELECT count(*) FROM auditcore.deliveries
                 WHERE tenant_id=:tenant_id
                   AND journey_id = ANY(CAST(:journey_ids AS uuid[]))) AS deliveries,
              (SELECT count(*) FROM auditcore.workflow_tasks
                 WHERE tenant_id=:tenant_id
                   AND journey_id = ANY(CAST(:journey_ids AS uuid[]))) AS workflow_tasks
            """
        ),
        params,
    ).mappings().one()
    return JourneyHousekeepingPreview(
        tenantId=tenant_id,
        scope=scope,
        scopeId=scope_id,
        journeys=int(row["journeys"]),
        customers=int(row["customers"]),
        evidence=int(row["evidence"]),
        diDocuments=len(
            _di_document_ids(
                connection,
                tenant_id=tenant_id,
                journey_ids=journey_ids,
            )
        ),
        auditFindings=int(row["findings"]),
        payments=int(row["payments"]),
        deliveries=int(row["deliveries"]),
        workflowTasks=int(row["workflow_tasks"]),
    )


def _purge_di_documents(
    *,
    tenant_id: str,
    document_ids: list[UUID],
    human_token: str,
) -> tuple[int, int]:
    if not document_ids:
        return 0, 0
    deleted_documents = 0
    deleted_storage_objects = 0
    try:
        with httpx.Client(base_url=_di_base_url().rstrip("/"), timeout=30.0) as client:
            for offset in range(0, len(document_ids), _DI_BATCH_SIZE):
                batch = document_ids[offset : offset + _DI_BATCH_SIZE]
                response = client.post(
                    f"/v1/tenants/{tenant_id}/admin/housekeeping/document-data/purge",
                    headers={"Authorization": f"Bearer {human_token}"},
                    json={
                        "confirmTenantId": tenant_id,
                        "confirmation": "PURGE_SELECTED_DOCUMENTS",
                        "documentIds": [str(document_id) for document_id in batch],
                    },
                )
                if not response.is_success:
                    raise RuntimeError(
                        f"DI housekeeping returned HTTP {response.status_code}"
                    )
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("errorCode") != "000":
                    raise RuntimeError("DI housekeeping returned an unsuccessful response")
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise TypeError("DI housekeeping returned invalid data")
                deleted_documents += int(data.get("deletedDocuments", 0))
                deleted_storage_objects += int(data.get("deletedStorageObjects", 0))
    except (httpx.HTTPError, ValueError, TypeError, RuntimeError) as exc:
        raise DependencyUnavailableError(
            detail=(
                "Journey housekeeping could not remove linked Document Intelligence data. "
                "Audit Core data was not deleted; retry the housekeeping operation."
            )
        ) from exc
    return deleted_documents, deleted_storage_objects


def _purge_audit_core_journeys(
    connection: Connection,
    *,
    tenant_id: str,
    journey_ids: list[UUID],
) -> tuple[int, int, int]:
    receipt = connection.execute(
        text(
            "SELECT auditcore.hard_delete_journey_transactions("
            ":tenant_id, CAST(:journey_ids AS uuid[]))"
        ),
        {"tenant_id": tenant_id, "journey_ids": journey_ids},
    ).scalar_one()
    if not isinstance(receipt, dict):
        raise TypeError("Audit Core Journey housekeeping returned invalid data")
    return (
        int(receipt.get("deletedJourneys", 0)),
        int(receipt.get("deletedCustomers", 0)),
        int(receipt.get("deletedEvidence", 0)),
    )


@router.get("/preview", response_model=JourneyHousekeepingPreview)
def preview_journey_housekeeping(
    tenant_id: str,
    scope: Annotated[HousekeepingScope, Query()],
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
    outletId: UUID | None = None,
    journeyId: UUID | None = None,
) -> JourneyHousekeepingPreview:
    del admin_request
    scope_id = _scope_id(
        tenant_id=tenant_id,
        scope=scope,
        outlet_id=outletId,
        journey_id=journeyId,
    )
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journey_ids = _target_journeys(
            connection,
            tenant_id=tenant_id,
            scope=scope,
            outlet_id=outletId,
            journey_id=journeyId,
        )
        return _preview(
            connection,
            tenant_id=tenant_id,
            scope=scope,
            scope_id=scope_id,
            journey_ids=journey_ids,
        )


@router.post("/purge", response_model=JourneyHousekeepingResult)
def purge_journey_housekeeping(
    tenant_id: str,
    command: JourneyHousekeepingCommand,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> JourneyHousekeepingResult:
    scope_id = _scope_id(
        tenant_id=tenant_id,
        scope=command.scope,
        outlet_id=command.outletId,
        journey_id=command.journeyId,
    )
    if command.confirmScopeId != scope_id:
        raise HTTPException(
            status_code=400,
            detail="Housekeeping confirmation does not match scope.",
        )
    if command.confirmation != _CONFIRMATION:
        raise HTTPException(status_code=400, detail="Invalid housekeeping confirmation.")

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journey_ids = _target_journeys(
            connection,
            tenant_id=tenant_id,
            scope=command.scope,
            outlet_id=command.outletId,
            journey_id=command.journeyId,
        )
        document_ids = _di_document_ids(
            connection,
            tenant_id=tenant_id,
            journey_ids=journey_ids,
        )

    deleted_di_documents, deleted_di_storage = _purge_di_documents(
        tenant_id=tenant_id,
        document_ids=document_ids,
        human_token=admin_request.bearer_token,
    )

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        deleted_journeys, deleted_customers, deleted_evidence = (
            _purge_audit_core_journeys(
                connection,
                tenant_id=tenant_id,
                journey_ids=journey_ids,
            )
        )

    return JourneyHousekeepingResult(
        tenantId=tenant_id,
        scope=command.scope,
        scopeId=scope_id,
        purgeStatus="REMOVED",
        deletedJourneys=deleted_journeys,
        deletedCustomers=deleted_customers,
        deletedEvidence=deleted_evidence,
        deletedDiDocuments=deleted_di_documents,
        deletedDiStorageObjects=deleted_di_storage,
        masterDataPreserved=True,
    )
