from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.authorization import require_tenant
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["journeys"])


class JourneyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    journeyReference: str | None = Field(default=None, max_length=160)
    observedStatusCode: str | None = Field(default=None, max_length=100)
    observedStatusSource: Literal[
        "EVIDENCE", "OPERATIONAL_INPUT", "SOURCE_SYSTEM", "CALCULATED"
    ] | None = None
    documentRequirementProfileVersionId: UUID | None = None
    policyVersionId: UUID | None = None
    priceListVersionId: UUID | None = None


class JourneyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    journeyReference: str | None = Field(default=None, max_length=160)
    observedStatusCode: str | None = Field(default=None, max_length=100)
    observedStatusSource: Literal[
        "EVIDENCE", "OPERATIONAL_INPUT", "SOURCE_SYSTEM", "CALCULATED"
    ] | None = None


class JourneyResponse(BaseModel):
    journeyId: UUID
    customerId: UUID
    dealerId: UUID
    outletId: UUID
    journeyReference: str | None
    observedStatusCode: str | None
    observedStatusSource: str | None
    auditState: str
    auditOutcome: str
    documentRequirementProfileVersionId: UUID | None
    policyVersionId: UUID | None
    priceListVersionId: UUID | None


def _scope(connection: Connection, principal: Principal, tenant_id: str) -> None:
    require_tenant(principal, tenant_id)
    set_tenant_context(connection, tenant_id)


def _not_found(resource: str) -> NotFoundError:
    return NotFoundError(
        error_code="VAC-NF-004" if resource == "Customer" else "VAC-NF-005",
        title=f"{resource} not found",
        detail=f"{resource} not found for the requested tenant.",
    )


def _journey_response(row) -> JourneyResponse:
    return JourneyResponse(
        journeyId=row["journey_id"],
        customerId=row["customer_id"],
        dealerId=row["dealer_id"],
        outletId=row["outlet_id"],
        journeyReference=row["journey_reference"],
        observedStatusCode=row["observed_status_code"],
        observedStatusSource=row["observed_status_source"],
        auditState=row["audit_state"],
        auditOutcome=row["audit_outcome"],
        documentRequirementProfileVersionId=row[
            "document_requirement_profile_version_id"
        ],
        policyVersionId=row["policy_version_id"],
        priceListVersionId=row["price_list_version_id"],
    )


def _customer_scope(connection: Connection, tenant_id: str, customer_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id
            FROM auditcore.customers
            WHERE tenant_id = :tenant_id AND customer_id = :customer_id
              AND status = 'ACTIVE'
            """
        ),
        {"tenant_id": tenant_id, "customer_id": customer_id},
    ).mappings().one_or_none()
    if row is None:
        raise _not_found("Customer")
    return row


def _require_published_versions(
    connection: Connection,
    *,
    tenant_id: str,
    document_profile_version_id: UUID | None,
    policy_version_id: UUID | None,
    price_list_version_id: UUID | None,
) -> None:
    checks = (
        (
            "document_requirement_profile_versions",
            "document_requirement_profile_version_id",
            document_profile_version_id,
        ),
        ("project_policy_versions", "policy_version_id", policy_version_id),
        ("price_list_versions", "price_list_version_id", price_list_version_id),
    )
    for table, id_column, version_id in checks:
        if version_id is None:
            continue
        exists = connection.execute(
            text(
                f"SELECT 1 FROM auditcore.{table} "
                f"WHERE tenant_id = :tenant_id AND {id_column} = :version_id "
                "AND lifecycle_status = 'PUBLISHED'"
            ),
            {"tenant_id": tenant_id, "version_id": version_id},
        ).scalar_one_or_none()
        if exists is None:
            raise AuditCoreError(
                error_code="VAC-MASTER-002",
                status_code=422,
                title="No effective master version",
                detail="Journey master reference must identify a published Tenant version.",
            )


@router.post(
    "/customers/{customer_id}/journeys",
    response_model=JourneyResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_journey(
    tenant_id: str,
    customer_id: UUID,
    payload: JourneyCreate,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> JourneyResponse:
    _scope(connection, principal, tenant_id)
    customer = _customer_scope(connection, tenant_id, customer_id)
    _require_published_versions(
        connection,
        tenant_id=tenant_id,
        document_profile_version_id=payload.documentRequirementProfileVersionId,
        policy_version_id=payload.policyVersionId,
        price_list_version_id=payload.priceListVersionId,
    )
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.journeys (
                tenant_id, dealer_id, outlet_id, customer_id,
                journey_reference, observed_status_code, observed_status_source,
                document_requirement_profile_version_id, policy_version_id,
                price_list_version_id, created_by_actor_id
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_id, :customer_id,
                :journey_reference, :observed_status_code, :observed_status_source,
                :document_profile_version_id, :policy_version_id,
                :price_list_version_id, :actor_id
            )
            RETURNING journey_id, customer_id, dealer_id, outlet_id,
                      journey_reference, observed_status_code, observed_status_source,
                      audit_state, audit_outcome,
                      document_requirement_profile_version_id, policy_version_id,
                      price_list_version_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": customer["dealer_id"],
            "outlet_id": customer["outlet_id"],
            "customer_id": customer_id,
            "journey_reference": payload.journeyReference,
            "observed_status_code": payload.observedStatusCode,
            "observed_status_source": payload.observedStatusSource,
            "document_profile_version_id": payload.documentRequirementProfileVersionId,
            "policy_version_id": payload.policyVersionId,
            "price_list_version_id": payload.priceListVersionId,
            "actor_id": principal.subject,
        },
    ).mappings().one()
    return _journey_response(row)


@router.get("/customers/{customer_id}/journeys", response_model=list[JourneyResponse])
def list_journeys(
    tenant_id: str,
    customer_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[JourneyResponse]:
    _scope(connection, principal, tenant_id)
    _customer_scope(connection, tenant_id, customer_id)
    rows = connection.execute(
        text(
            """
            SELECT journey_id, customer_id, dealer_id, outlet_id,
                   journey_reference, observed_status_code, observed_status_source,
                   audit_state, audit_outcome,
                   document_requirement_profile_version_id, policy_version_id,
                   price_list_version_id
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND customer_id = :customer_id
            ORDER BY created_at_utc, journey_id
            """
        ),
        {"tenant_id": tenant_id, "customer_id": customer_id},
    ).mappings()
    return [_journey_response(row) for row in rows]


@router.get("/journeys/{journey_id}", response_model=JourneyResponse)
def get_journey(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> JourneyResponse:
    _scope(connection, principal, tenant_id)
    row = connection.execute(
        text(
            """
            SELECT journey_id, customer_id, dealer_id, outlet_id,
                   journey_reference, observed_status_code, observed_status_source,
                   audit_state, audit_outcome,
                   document_requirement_profile_version_id, policy_version_id,
                   price_list_version_id
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise _not_found("Journey")
    return _journey_response(row)


@router.patch("/journeys/{journey_id}", response_model=JourneyResponse)
def patch_journey(
    tenant_id: str,
    journey_id: UUID,
    payload: JourneyPatch,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> JourneyResponse:
    _scope(connection, principal, tenant_id)
    row = connection.execute(
        text(
            """
            UPDATE auditcore.journeys
            SET journey_reference = COALESCE(:journey_reference, journey_reference),
                observed_status_code = COALESCE(:observed_status_code, observed_status_code),
                observed_status_source = COALESCE(:observed_status_source, observed_status_source),
                updated_by_actor_id = :actor_id,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            RETURNING journey_id, customer_id, dealer_id, outlet_id,
                      journey_reference, observed_status_code, observed_status_source,
                      audit_state, audit_outcome,
                      document_requirement_profile_version_id, policy_version_id,
                      price_list_version_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "journey_reference": payload.journeyReference,
            "observed_status_code": payload.observedStatusCode,
            "observed_status_source": payload.observedStatusSource,
            "actor_id": principal.subject,
        },
    ).mappings().one_or_none()
    if row is None:
        raise _not_found("Journey")
    return _journey_response(row)
