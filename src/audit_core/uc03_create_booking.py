from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, AuthorizationError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_commands import _append_workflow_event, _authorize_security

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-create-booking"])


class CreateBookingOutlet(BaseModel):
    outletId: UUID
    outletName: str
    outletClassification: str
    dealerId: UUID
    dealerName: str


class CreateBookingContext(BaseModel):
    outlets: list[CreateBookingOutlet]


class CreateBookingCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outletId: UUID | None = None


class CreateBookingResponse(BaseModel):
    journeyId: UUID
    customerId: UUID
    dealerId: UUID
    outletId: UUID
    businessStatus: str
    aggregateVersion: int


def _authorize_pc(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
) -> list[dict]:
    rows = list(
        connection.execute(
            text(
                """
                SELECT DISTINCT
                    o.outlet_id,
                    o.outlet_name,
                    o.outlet_classification,
                    d.dealer_id,
                    d.dealer_name
                FROM auditcore.dealer_outlets o
                JOIN auditcore.dealers d
                  ON d.tenant_id=o.tenant_id AND d.dealer_id=o.dealer_id
                WHERE o.tenant_id=:tenant_id
                  AND o.status='ACTIVE'
                  AND d.status='ACTIVE'
                  AND EXISTS (
                      SELECT 1
                      FROM auditcore.business_assignments ba
                      WHERE ba.tenant_id=o.tenant_id
                        AND ba.security_actor_id=:actor_id
                        AND ba.business_role_code='PC'
                        AND ba.assignment_status='ACTIVE'
                        AND ba.effective_from <= now()
                        AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                        AND (ba.dealer_id IS NULL OR ba.dealer_id=o.dealer_id)
                        AND (ba.outlet_id IS NULL OR ba.outlet_id=o.outlet_id)
                  )
                ORDER BY lower(d.dealer_name), lower(o.outlet_name), o.outlet_id
                """
            ),
            {"tenant_id": tenant_id, "actor_id": actor_id},
        ).mappings()
    )
    if not rows:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )
    return [dict(row) for row in rows]


def _effective_project_versions(connection: Connection, *, tenant_id: str) -> dict[str, UUID | None]:
    project = connection.execute(
        text(
            """
            SELECT timezone_name
            FROM auditcore.projects
            WHERE tenant_id=:tenant_id AND project_status='ACTIVE'
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if project is None:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Project is not active",
            detail="Create Booking is available only for an active Project.",
        )

    business_date = connection.execute(
        text("SELECT (now() AT TIME ZONE :timezone_name)::date"),
        {"timezone_name": project["timezone_name"]},
    ).scalar_one()

    document_profile_version_id = connection.execute(
        text(
            """
            SELECT document_requirement_profile_version_id
            FROM auditcore.document_requirement_profile_versions
            WHERE tenant_id=:tenant_id
              AND lifecycle_status='PUBLISHED'
              AND effective_from <= :business_date
            ORDER BY effective_from DESC, version_no DESC,
                     document_requirement_profile_version_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "business_date": business_date},
    ).scalar_one_or_none()

    policy_version_id = connection.execute(
        text(
            """
            SELECT policy_version_id
            FROM auditcore.project_policy_versions
            WHERE tenant_id=:tenant_id
              AND lifecycle_status='PUBLISHED'
              AND effective_from <= :business_date
            ORDER BY effective_from DESC, version_no DESC, policy_version_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "business_date": business_date},
    ).scalar_one_or_none()

    price_list_version_id = connection.execute(
        text(
            """
            SELECT price_list_version_id
            FROM auditcore.price_list_versions
            WHERE tenant_id=:tenant_id
              AND lifecycle_status='PUBLISHED'
              AND effective_from <= :business_date
              AND (effective_to IS NULL OR effective_to >= :business_date)
            ORDER BY effective_from DESC, version_no DESC, price_list_version_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "business_date": business_date},
    ).scalar_one_or_none()

    return {
        "document_profile_version_id": document_profile_version_id,
        "policy_version_id": policy_version_id,
        "price_list_version_id": price_list_version_id,
    }


def _scope(
    connection: Connection,
    *,
    tenant_id: str,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
) -> list[dict]:
    _authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    return _authorize_pc(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
    )


@router.get("/create-booking-context", response_model=CreateBookingContext)
def get_create_booking_context(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> CreateBookingContext:
    outlets = _scope(
        connection,
        tenant_id=tenant_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return CreateBookingContext(
        outlets=[
            CreateBookingOutlet(
                outletId=row["outlet_id"],
                outletName=row["outlet_name"],
                outletClassification=row["outlet_classification"],
                dealerId=row["dealer_id"],
                dealerName=row["dealer_name"],
            )
            for row in outlets
        ]
    )


@router.post(
    "/bookings",
    response_model=CreateBookingResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_booking(
    tenant_id: str,
    payload: CreateBookingCommand,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> CreateBookingResponse:
    outlets = _scope(
        connection,
        tenant_id=tenant_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )

    if payload.outletId is None:
        if len(outlets) != 1:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Outlet selection required",
                detail="Select one of your authorized outlets before creating the Booking.",
            )
        selected = outlets[0]
    else:
        selected = next(
            (row for row in outlets if row["outlet_id"] == payload.outletId),
            None,
        )
        if selected is None:
            raise AuthorizationError(
                error_code="VAC-AUTH-002",
                status_code=403,
                title="Permission denied",
            )

    versions = _effective_project_versions(connection, tenant_id=tenant_id)

    def execute() -> dict:
        customer = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code,
                    display_name, created_by_actor_id
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'PENDING',
                    'Customer pending', :actor_id
                )
                RETURNING customer_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": selected["dealer_id"],
                "outlet_id": selected["outlet_id"],
                "actor_id": human_principal.subject,
            },
        ).mappings().one()

        journey = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id,
                    document_requirement_profile_version_id,
                    policy_version_id, price_list_version_id,
                    created_by_actor_id
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id,
                    :document_profile_version_id,
                    :policy_version_id, :price_list_version_id,
                    :actor_id
                )
                RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": selected["dealer_id"],
                "outlet_id": selected["outlet_id"],
                "customer_id": customer["customer_id"],
                "document_profile_version_id": versions["document_profile_version_id"],
                "policy_version_id": versions["policy_version_id"],
                "price_list_version_id": versions["price_list_version_id"],
                "actor_id": human_principal.subject,
            },
        ).mappings().one()

        stage = connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status,
                    audit_state, audit_status, first_started_at_utc,
                    latest_activity_at_utc, version_no
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', 'BOOKING_STARTED',
                    'NOT_STARTED', 'NOT_EVALUATED', now(), now(), 1
                )
                RETURNING business_status, version_no
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey["journey_id"]},
        ).mappings().one()

        _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey["journey_id"],
            event_type="BOOKING_CREATED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot="PC",
            idempotency_key=idempotency_key,
            correlation_id=idempotency_key,
            safe_payload={"outletId": str(selected["outlet_id"])},
            aggregate_version=int(stage["version_no"]),
        )

        return {
            "journeyId": str(journey["journey_id"]),
            "customerId": str(customer["customer_id"]),
            "dealerId": str(selected["dealer_id"]),
            "outletId": str(selected["outlet_id"]),
            "businessStatus": stage["business_status"],
            "aggregateVersion": int(stage["version_no"]),
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key="uc03.booking.create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
        response_status=201,
    )
    return CreateBookingResponse.model_validate(body)
