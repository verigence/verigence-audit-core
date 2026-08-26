from __future__ import annotations

import os
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import HumanAdminRequest, get_engine, require_super_admin_request
from audit_core.errors import DependencyUnavailableError, NotFoundError

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/admin/housekeeping/journeys",
    tags=["journey-housekeeping"],
)

_SCOPE = Literal["TENANT", "OUTLET", "JOURNEY"]
_CONFIRMATION = "PURGE_JOURNEY_DATA"
_DI_BATCH_SIZE = 1000


class JourneyHousekeepingPreview(BaseModel):
    tenantId: str
    scope: _SCOPE
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
    scope: _SCOPE
    outletId: UUID | None = None
    journeyId: UUID | None = None
    confirmScopeId: str
    confirmation: Literal["PURGE_JOURNEY_DATA"]


class JourneyHousekeepingResult(BaseModel):
    tenantId: str
    scope: _SCOPE
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
    scope: _SCOPE,
    outlet_id: UUID | None,
    journey_id: UUID | None,
) -> str:
    if scope == "TENANT":
        if outlet_id is not None or journey_id is not None:
            raise HTTPException(status_code=400, detail="Tenant scope cannot include Outlet/Journey ID.")
        return tenant_id
    if scope == "OUTLET":
        if outlet_id is None or journey_id is not None:
            raise HTTPException(status_code=400, detail="Outlet scope requires only outletId.")
        return str(outlet_id)
    if journey_id is None or outlet_id is not None:
        raise HTTPException(status_code=400, detail="Journey scope requires only journeyId.")
    return str(journey_id)


def _target_journeys(
    connection: Connection,
    *,
    tenant_id: str,
    scope: _SCOPE,
    outlet_id: UUID | None,
    journey_id: UUID | None,
) -> list[UUID]:
    if scope == "TENANT":
        rows = connection.execute(
            text(
                "SELECT journey_id FROM auditcore.journeys "
                "WHERE tenant_id=:tenant_id ORDER BY created_at_utc, journey_id"
            ),
            {"tenant_id": tenant_id},
        ).scalars().all()
        return list(rows)

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
        rows = connection.execute(
            text(
                "SELECT journey_id FROM auditcore.journeys "
                "WHERE tenant_id=:tenant_id AND outlet_id=:outlet_id "
                "ORDER BY created_at_utc, journey_id"
            ),
            {"tenant_id": tenant_id, "outlet_id": outlet_id},
        ).scalars().all()
        return list(rows)

    assert journey_id is not None
    exists = connection.execute(
        text(
            "SELECT journey_id FROM auditcore.journeys "
            "WHERE tenant_id=:tenant_id AND journey_id=:journey_id"
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    if exists is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Journey not found",
            detail="Journey not found for the selected Project/Tenant.",
        )
    return [exists]


def _preview(
    connection: Connection,
    *,
    tenant_id: str,
    scope: _SCOPE,
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
              (SELECT count(DISTINCT di_document_id) FROM auditcore.evidence
                 WHERE tenant_id=:tenant_id
                   AND journey_id = ANY(CAST(:journey_ids AS uuid[]))
                   AND di_document_id IS NOT NULL) AS di_documents,
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
        diDocuments=int(row["di_documents"]),
        auditFindings=int(row["findings"]),
        payments=int(row["payments"]),
        deliveries=int(row["deliveries"]),
        workflowTasks=int(row["workflow_tasks"]),
    )


def _di_documents(
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
                FROM auditcore.evidence
                WHERE tenant_id=:tenant_id
                  AND journey_id = ANY(CAST(:journey_ids AS uuid[]))
                  AND di_document_id IS NOT NULL
                ORDER BY di_document_id
                """
            ),
            {"tenant_id": tenant_id, "journey_ids": journey_ids},
        ).scalars().all()
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
                if response.status_code < 200 or response.status_code >= 300:
                    raise RuntimeError(f"DI housekeeping returned HTTP {response.status_code}")
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("errorCode") != "000":
                    raise RuntimeError("DI housekeeping returned an unsuccessful response")
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise RuntimeError("DI housekeeping returned invalid data")
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


def _delete_audit_core_journeys(
    connection: Connection,
    *,
    tenant_id: str,
    journey_ids: list[UUID],
) -> tuple[int, int, int]:
    if not journey_ids:
        return 0, 0, 0

    params = {"tenant_id": tenant_id, "journey_ids": journey_ids}
    target = "journey_id = ANY(CAST(:journey_ids AS uuid[]))"
    customer_ids = list(
        connection.execute(
            text(
                f"SELECT DISTINCT customer_id FROM auditcore.journeys "
                f"WHERE tenant_id=:tenant_id AND {target}"
            ),
            params,
        ).scalars().all()
    )
    evidence_ids = list(
        connection.execute(
            text(
                f"SELECT evidence_id FROM auditcore.evidence "
                f"WHERE tenant_id=:tenant_id AND {target}"
            ),
            params,
        ).scalars().all()
    )
    finding_ids = list(
        connection.execute(
            text(
                f"SELECT audit_finding_id FROM auditcore.audit_findings "
                f"WHERE tenant_id=:tenant_id AND {target}"
            ),
            params,
        ).scalars().all()
    )
    entity_ids = [str(value) for value in journey_ids + evidence_ids + finding_ids]

    # Break the only Customer -> Evidence cycle before evidence rows are removed.
    if evidence_ids:
        connection.execute(
            text(
                """
                UPDATE auditcore.customers
                SET legal_name_source_evidence_id=NULL,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id
                  AND legal_name_source_evidence_id = ANY(CAST(:evidence_ids AS uuid[]))
                """
            ),
            {"tenant_id": tenant_id, "evidence_ids": evidence_ids},
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.evidence
                SET supersedes_evidence_id=NULL
                WHERE tenant_id=:tenant_id
                  AND supersedes_evidence_id = ANY(CAST(:evidence_ids AS uuid[]))
                """
            ),
            {"tenant_id": tenant_id, "evidence_ids": evidence_ids},
        )

    # Second-level children first.
    connection.execute(
        text(
            """
            DELETE FROM auditcore.workflow_task_attempts
            WHERE tenant_id=:tenant_id
              AND workflow_task_id IN (
                    SELECT workflow_task_id FROM auditcore.workflow_tasks
                    WHERE tenant_id=:tenant_id
                      AND journey_id = ANY(CAST(:journey_ids AS uuid[]))
              )
            """
        ),
        params,
    )
    connection.execute(text(f"DELETE FROM auditcore.crm_interactions WHERE tenant_id=:tenant_id AND {target}"), params)
    connection.execute(text(f"DELETE FROM auditcore.workflow_dead_letters WHERE tenant_id=:tenant_id AND {target}"), params)
    connection.execute(text(f"DELETE FROM auditcore.workflow_task_events WHERE tenant_id=:tenant_id AND {target}"), params)
    connection.execute(text(f"DELETE FROM auditcore.workflow_tasks WHERE tenant_id=:tenant_id AND {target}"), params)
    connection.execute(text(f"DELETE FROM auditcore.workflow_instances WHERE tenant_id=:tenant_id AND {target}"), params)

    if finding_ids or evidence_ids:
        connection.execute(
            text(
                """
                DELETE FROM auditcore.finding_evidence
                WHERE tenant_id=:tenant_id
                  AND (
                       audit_finding_id = ANY(CAST(:finding_ids AS uuid[]))
                       OR evidence_id = ANY(CAST(:evidence_ids AS uuid[]))
                  )
                """
            ),
            {
                "tenant_id": tenant_id,
                "finding_ids": finding_ids,
                "evidence_ids": evidence_ids,
            },
        )
    if finding_ids:
        connection.execute(
            text(
                "DELETE FROM auditcore.finding_remarks WHERE tenant_id=:tenant_id "
                "AND audit_finding_id = ANY(CAST(:finding_ids AS uuid[]))"
            ),
            {"tenant_id": tenant_id, "finding_ids": finding_ids},
        )
    connection.execute(text(f"DELETE FROM auditcore.audit_finding_events WHERE tenant_id=:tenant_id AND {target}"), params)
    connection.execute(text(f"DELETE FROM auditcore.audit_findings WHERE tenant_id=:tenant_id AND {target}"), params)
    connection.execute(text(f"DELETE FROM auditcore.audit_evaluations WHERE tenant_id=:tenant_id AND {target}"), params)
    connection.execute(text(f"DELETE FROM auditcore.payment_verification_events WHERE tenant_id=:tenant_id AND {target}"), params)
    connection.execute(text(f"DELETE FROM auditcore.delivery_status_history WHERE tenant_id=:tenant_id AND {target}"), params)

    # All remaining Journey children that may also reference Evidence.
    direct_tables = (
        "activity_records",
        "audit_state_events",
        "bookings",
        "commercial_lines",
        "daily_ops_items",
        "deliveries",
        "discount_applications",
        "escalations",
        "evidence_facts",
        "evidence_ingestion_operations",
        "finance_records",
        "insurance_records",
        "journey_addons",
        "journey_capture_proposals",
        "journey_delivery_audit_facts",
        "journey_document_assessments",
        "journey_products",
        "journey_stage_states",
        "journey_workflow_events",
        "outbox_events",
        "payments",
        "registration_records",
        "review_decisions",
        "trade_in_cases",
        "vehicle_records",
    )
    for table_name in direct_tables:
        connection.execute(
            text(
                f"DELETE FROM auditcore.{table_name} "  # noqa: S608
                f"WHERE tenant_id=:tenant_id AND {target}"
            ),
            params,
        )

    # Non-FK transaction records can otherwise retain stale references/idempotent results.
    if entity_ids:
        connection.execute(
            text(
                "DELETE FROM auditcore.audit_events WHERE tenant_id=:tenant_id "
                "AND entity_id = ANY(CAST(:entity_ids AS text[]))"
            ),
            {"tenant_id": tenant_id, "entity_ids": entity_ids},
        )
        connection.execute(
            text(
                "DELETE FROM auditcore.audit_chain_heads WHERE tenant_id=:tenant_id "
                "AND entity_id = ANY(CAST(:entity_ids AS text[]))"
            ),
            {"tenant_id": tenant_id, "entity_ids": entity_ids},
        )
    connection.execute(
        text(
            """
            DELETE FROM auditcore.inbox_events
            WHERE tenant_id=:tenant_id
              AND COALESCE(event_payload->>'journeyId', event_payload->>'journey_id')
                    = ANY(CAST(:journey_text_ids AS text[]))
            """
        ),
        {"tenant_id": tenant_id, "journey_text_ids": [str(value) for value in journey_ids]},
    )
    connection.execute(
        text(
            """
            DELETE FROM auditcore.idempotency_records
            WHERE tenant_id=:tenant_id
              AND (
                   COALESCE(response_body->>'journeyId', response_body->>'journey_id')
                     = ANY(CAST(:journey_text_ids AS text[]))
                   OR logical_result_id = ANY(CAST(:entity_ids AS text[]))
              )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_text_ids": [str(value) for value in journey_ids],
            "entity_ids": entity_ids,
        },
    )

    if evidence_ids:
        connection.execute(
            text(
                "DELETE FROM auditcore.evidence WHERE tenant_id=:tenant_id "
                "AND evidence_id = ANY(CAST(:evidence_ids AS uuid[]))"
            ),
            {"tenant_id": tenant_id, "evidence_ids": evidence_ids},
        )
    connection.execute(text(f"DELETE FROM auditcore.journey_document_requirements WHERE tenant_id=:tenant_id AND {target}"), params)
    deleted_journeys = connection.execute(
        text(f"DELETE FROM auditcore.journeys WHERE tenant_id=:tenant_id AND {target}"),
        params,
    ).rowcount or 0

    deleted_customers = 0
    if customer_ids:
        orphan_ids = list(
            connection.execute(
                text(
                    """
                    SELECT c.customer_id
                    FROM auditcore.customers c
                    WHERE c.tenant_id=:tenant_id
                      AND c.customer_id = ANY(CAST(:customer_ids AS uuid[]))
                      AND NOT EXISTS (
                            SELECT 1 FROM auditcore.journeys j
                            WHERE j.tenant_id=c.tenant_id AND j.customer_id=c.customer_id
                      )
                    """
                ),
                {"tenant_id": tenant_id, "customer_ids": customer_ids},
            ).scalars().all()
        )
        if orphan_ids:
            orphan_params = {"tenant_id": tenant_id, "customer_ids": orphan_ids}
            connection.execute(
                text(
                    "DELETE FROM auditcore.di_subject_mappings WHERE tenant_id=:tenant_id "
                    "AND customer_id = ANY(CAST(:customer_ids AS uuid[]))"
                ),
                orphan_params,
            )
            connection.execute(
                text(
                    "DELETE FROM auditcore.customer_identity_index WHERE tenant_id=:tenant_id "
                    "AND customer_id = ANY(CAST(:customer_ids AS uuid[]))"
                ),
                orphan_params,
            )
            connection.execute(
                text(
                    "DELETE FROM auditcore.audit_events WHERE tenant_id=:tenant_id "
                    "AND entity_id = ANY(CAST(:customer_text_ids AS text[]))"
                ),
                {"tenant_id": tenant_id, "customer_text_ids": [str(value) for value in orphan_ids]},
            )
            connection.execute(
                text(
                    "DELETE FROM auditcore.audit_chain_heads WHERE tenant_id=:tenant_id "
                    "AND entity_id = ANY(CAST(:customer_text_ids AS text[]))"
                ),
                {"tenant_id": tenant_id, "customer_text_ids": [str(value) for value in orphan_ids]},
            )
            deleted_customers = connection.execute(
                text(
                    "DELETE FROM auditcore.customers WHERE tenant_id=:tenant_id "
                    "AND customer_id = ANY(CAST(:customer_ids AS uuid[]))"
                ),
                orphan_params,
            ).rowcount or 0

    return int(deleted_journeys), int(deleted_customers), len(evidence_ids)


@router.get("/preview", response_model=JourneyHousekeepingPreview)
def preview_journey_housekeeping(
    tenant_id: str,
    scope: Annotated[_SCOPE, Query()],
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
        raise HTTPException(status_code=400, detail="Housekeeping confirmation does not match scope.")
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
        preview = _preview(
            connection,
            tenant_id=tenant_id,
            scope=command.scope,
            scope_id=scope_id,
            journey_ids=journey_ids,
        )
        document_ids = _di_documents(
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
        deleted_journeys, deleted_customers, deleted_evidence = _delete_audit_core_journeys(
            connection,
            tenant_id=tenant_id,
            journey_ids=journey_ids,
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
