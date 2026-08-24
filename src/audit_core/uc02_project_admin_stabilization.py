from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dealers import (
    OutletResponse,
    _dealer_exists,
    _dealer_impact,
    _not_found,
    _outlet_impact,
    _outlet_response,
)
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_project_admin_request,
)
from audit_core.errors import BusinessValidationError, ConflictError, NotFoundError
from audit_core.idempotency import execute_idempotent_json_command

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["uc02-project-admin-stabilization"])


def _project_status(connection: Connection, tenant_id: str) -> str:
    value = connection.execute(
        text("SELECT project_status FROM auditcore.projects WHERE tenant_id=:tenant_id"),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    if value is None:
        raise NotFoundError(
            error_code="VAC-NF-001",
            title="Project not found",
            detail="Project not found for the requested tenant.",
        )
    return str(value)


@router.get("/outlets", response_model=list[OutletResponse])
def list_project_outlets(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[OutletResponse]:
    """Return the complete Project outlet hierarchy in one read.

    UC02 previously made one outlet request per Dealer. This additive read path keeps
    the existing per-Dealer contract intact while removing the browser-side N+1 fan-out.
    """
    del admin_request
    set_tenant_context(connection, tenant_id)
    rows = connection.execute(
        text(
            """
            SELECT outlet_id, dealer_id, outlet_code, outlet_name,
                   outlet_classification, address_text, city, state_region, postal_code,
                   google_place_id, latitude, longitude, monthly_vehicle_volume,
                   status, version_no
            FROM auditcore.dealer_outlets
            WHERE tenant_id=:tenant_id
            ORDER BY dealer_id, outlet_code
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings()
    return [_outlet_response(row) for row in rows]


def _blocking_setup_dependencies(
    connection: Connection,
    *,
    tenant_id: str,
    dealer_id: UUID,
) -> dict[str, int]:
    """Return dependencies that make a setup Dealer unsafe to remove.

    The outlet row itself is setup structure and is intentionally not a blocker while
    the Project is CONFIGURING. Everything linked to those outlets remains a blocker.
    """
    dealer_dependencies = _dealer_impact(connection, tenant_id, dealer_id)
    blockers = {
        key: value
        for key, value in dealer_dependencies.items()
        if key != "outlets" and value > 0
    }
    outlet_ids = connection.execute(
        text(
            "SELECT outlet_id FROM auditcore.dealer_outlets "
            "WHERE tenant_id=:tenant_id AND dealer_id=:dealer_id"
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id},
    ).scalars().all()
    for outlet_id in outlet_ids:
        impact = _outlet_impact(connection, tenant_id, dealer_id, UUID(str(outlet_id)))
        for key, value in impact.items():
            if value:
                blockers[key] = blockers.get(key, 0) + int(value)
    return blockers


def _dependency_message(blockers: dict[str, int]) -> str:
    labels = {
        "businessAssignments": "role mappings",
        "discountEligibility": "discount eligibility records",
        "workflowTasks": "workflow tasks",
        "dealershipStaff": "dealership staff",
        "customers": "customers",
        "journeys": "journeys",
        "dailyOpsRuns": "daily operations runs",
        "activityRecords": "activity records",
        "pcDailyNotes": "PC daily notes",
    }
    details = [
        f"{labels.get(key, key)}: {value}"
        for key, value in sorted(blockers.items())
        if value
    ]
    return (
        "Dealer cannot be removed because Project data is already linked to it. "
        "Remove the dependent setup first: " + ", ".join(details) + "."
    )


@router.delete(
    "/dealers/{dealer_id}/setup",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an unused Dealer and its empty setup outlets",
)
def delete_configuring_dealer_setup(
    tenant_id: str,
    dealer_id: UUID,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> Response:
    """Delete mistaken Dealer setup safely while a Project is still CONFIGURING.

    Empty Dealer Outlet rows may be deleted with the Dealer. Any business assignment,
    customer, journey, workflow, master eligibility or operational record still blocks
    the action. Active Projects retain the existing conservative delete semantics.
    """
    del admin_request
    set_tenant_context(connection, tenant_id)
    if _project_status(connection, tenant_id) != "CONFIGURING":
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Dealer setup cannot be removed",
            detail="Dealer setup can be removed only while the Project is CONFIGURING.",
        )

    def perform_delete() -> dict[str, object]:
        if not _dealer_exists(connection, tenant_id, dealer_id):
            raise _not_found("Dealer")
        blockers = _blocking_setup_dependencies(
            connection,
            tenant_id=tenant_id,
            dealer_id=dealer_id,
        )
        if blockers:
            raise BusinessValidationError(detail=_dependency_message(blockers))
        connection.execute(
            text(
                "DELETE FROM auditcore.dealer_outlets "
                "WHERE tenant_id=:tenant_id AND dealer_id=:dealer_id"
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id},
        )
        connection.execute(
            text(
                "DELETE FROM auditcore.dealers "
                "WHERE tenant_id=:tenant_id AND dealer_id=:dealer_id"
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id},
        )
        return {}

    execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key="UC02_CONFIGURING_DEALER_SETUP_DELETE",
        idempotency_key=idempotency_key,
        request_payload={"dealerId": str(dealer_id)},
        execute=perform_delete,
        response_status=status.HTTP_204_NO_CONTENT,
        logical_result_id=str(dealer_id),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
