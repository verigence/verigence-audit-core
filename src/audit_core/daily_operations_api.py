from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.daily_operations import complete_daily_ops_run, create_daily_ops_run
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.security import Principal

router = APIRouter(tags=["daily-operations"])


class DailyOpsCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    businessDate: date


class DailyOpsResponse(BaseModel):
    runId: UUID
    outletId: UUID
    businessDate: date
    pcActorId: str
    status: str
    startedAtUtc: datetime
    completedAtUtc: datetime | None
    versionNo: int


def _response(row) -> DailyOpsResponse:
    return DailyOpsResponse(
        runId=row["daily_ops_run_id"],
        outletId=row["outlet_id"],
        businessDate=row["business_date"],
        pcActorId=row["pc_actor_id"],
        status=row["run_status"],
        startedAtUtc=row["started_at_utc"],
        completedAtUtc=row["completed_at_utc"],
        versionNo=row["version_no"],
    )


def _run(connection: Connection, *, tenant_id: str, run_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT daily_ops_run_id, outlet_id, business_date, pc_actor_id,
                   run_status, started_at_utc, completed_at_utc, version_no
            FROM auditcore.daily_ops_runs
            WHERE tenant_id = :tenant_id AND daily_ops_run_id = :run_id
            """
        ),
        {"tenant_id": tenant_id, "run_id": run_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Daily operations run not found",
            detail="Daily operations run not found for the requested tenant.",
        )
    return row


@router.post(
    "/v1/tenants/{tenant_id}/outlets/{outlet_id}/daily-ops",
    response_model=DailyOpsResponse,
    status_code=201,
)
def create_daily_run(
    tenant_id: str,
    outlet_id: UUID,
    payload: DailyOpsCreateInput,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DailyOpsResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.daily_ops.execute")
    set_tenant_context(connection, tenant_id)
    outlet = connection.execute(
        text(
            """
            SELECT dealer_id
            FROM auditcore.dealer_outlets
            WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id
            """
        ),
        {"tenant_id": tenant_id, "outlet_id": outlet_id},
    ).mappings().one_or_none()
    if outlet is None:
        raise NotFoundError(
            error_code="VAC-NF-003",
            title="Outlet not found",
            detail="Outlet not found for the requested tenant.",
        )
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=outlet["dealer_id"],
        outlet_id=outlet_id,
    )
    run_id = create_daily_ops_run(
        connection,
        tenant_id=tenant_id,
        outlet_id=outlet_id,
        business_date=payload.businessDate,
        pc_actor_id=principal.subject,
    )
    return _response(_run(connection, tenant_id=tenant_id, run_id=run_id))


@router.get(
    "/v1/tenants/{tenant_id}/outlets/{outlet_id}/daily-ops",
    response_model=list[DailyOpsResponse],
)
def list_daily_runs(
    tenant_id: str,
    outlet_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[DailyOpsResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.daily_ops.read")
    set_tenant_context(connection, tenant_id)
    outlet = connection.execute(
        text(
            """
            SELECT dealer_id
            FROM auditcore.dealer_outlets
            WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id
            """
        ),
        {"tenant_id": tenant_id, "outlet_id": outlet_id},
    ).mappings().one_or_none()
    if outlet is None:
        raise NotFoundError(
            error_code="VAC-NF-003",
            title="Outlet not found",
            detail="Outlet not found for the requested tenant.",
        )
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=outlet["dealer_id"],
        outlet_id=outlet_id,
    )
    rows = connection.execute(
        text(
            """
            SELECT daily_ops_run_id, outlet_id, business_date, pc_actor_id,
                   run_status, started_at_utc, completed_at_utc, version_no
            FROM auditcore.daily_ops_runs
            WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id
            ORDER BY business_date DESC, started_at_utc DESC
            """
        ),
        {"tenant_id": tenant_id, "outlet_id": outlet_id},
    ).mappings().all()
    return [_response(row) for row in rows]


@router.get(
    "/v1/tenants/{tenant_id}/daily-ops/{run_id}",
    response_model=DailyOpsResponse,
)
def read_daily_run(
    tenant_id: str,
    run_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DailyOpsResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.daily_ops.read")
    set_tenant_context(connection, tenant_id)
    row = _run(connection, tenant_id=tenant_id, run_id=run_id)
    outlet = connection.execute(
        text(
            """
            SELECT dealer_id
            FROM auditcore.dealer_outlets
            WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id
            """
        ),
        {"tenant_id": tenant_id, "outlet_id": row["outlet_id"]},
    ).mappings().one()
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=outlet["dealer_id"],
        outlet_id=row["outlet_id"],
    )
    return _response(row)


@router.post(
    "/v1/tenants/{tenant_id}/daily-ops/{run_id}/complete",
    response_model=DailyOpsResponse,
)
def complete_daily_run(
    tenant_id: str,
    run_id: UUID,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DailyOpsResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.daily_ops.execute")
    set_tenant_context(connection, tenant_id)
    current = _run(connection, tenant_id=tenant_id, run_id=run_id)
    outlet = connection.execute(
        text(
            """
            SELECT dealer_id
            FROM auditcore.dealer_outlets
            WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id
            """
        ),
        {"tenant_id": tenant_id, "outlet_id": current["outlet_id"]},
    ).mappings().one()
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=outlet["dealer_id"],
        outlet_id=current["outlet_id"],
    )

    def execute() -> dict:
        complete_daily_ops_run(
            connection,
            tenant_id=tenant_id,
            daily_ops_run_id=run_id,
        )
        response = _response(_run(connection, tenant_id=tenant_id, run_id=run_id))
        return response.model_dump(mode="json")

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"daily_ops.complete:{run_id}",
        idempotency_key=idempotency_key,
        request_payload={"runId": str(run_id)},
        execute=execute,
        logical_result_id=str(run_id),
    )
    return DailyOpsResponse.model_validate(body)
