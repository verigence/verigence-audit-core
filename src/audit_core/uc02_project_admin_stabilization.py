from __future__ import annotations

import os
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from audit_core.admin_operations import (
    claim_administrative_operation,
    update_administrative_operation,
)
from audit_core.db import set_tenant_context
from audit_core.dealers import _dealer_exists, _dealer_impact, _not_found, _outlet_impact
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_engine,
    require_project_admin_request,
    require_super_admin_request,
)
from audit_core.errors import (
    BusinessValidationError,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
)
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.project_provisioning import _delete_security_tenant, _security_base_url

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["uc02-project-admin-stabilization"])

_DEALER_LEVEL_DEPENDENCIES = frozenset(
    {"businessAssignments", "discountEligibility", "workflowTasks"}
)
_PROJECT_DELETE_TIMEOUT_SECONDS = 30.0


class ProjectDeletionImpactResponse(BaseModel):
    tenantId: str
    projectName: str
    projectStatus: str
    journeyCount: int
    canDelete: bool
    rule: str
    cleanupTargets: list[str]


class ProjectDeleteConfirmation(BaseModel):
    confirmProjectName: str = Field(min_length=1, max_length=240)


class ProjectDeleteResponse(BaseModel):
    operationId: str
    tenantId: str
    projectName: str
    projectStatus: str
    journeyCount: int
    deletionStatus: str
    diReceipt: dict[str, Any]
    securityReceipt: dict[str, Any]
    auditCoreReceipt: dict[str, Any]


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


def _blocking_setup_dependencies(
    connection: Connection,
    *,
    tenant_id: str,
    dealer_id: UUID,
) -> dict[str, int]:
    dealer_dependencies = _dealer_impact(connection, tenant_id, dealer_id)
    blockers = {
        key: int(value)
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
            if key in _DEALER_LEVEL_DEPENDENCIES:
                continue
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


def _project_delete_impact(connection: Connection, tenant_id: str):
    row = connection.execute(
        text(
            """
            SELECT p.project_name, p.project_status,
                   (SELECT count(*) FROM auditcore.journeys j
                    WHERE j.tenant_id=p.tenant_id) AS journey_count
            FROM auditcore.projects p
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


@router.get("/project/deletion-impact", response_model=ProjectDeletionImpactResponse)
def get_project_deletion_impact(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectDeletionImpactResponse:
    del admin_request
    set_tenant_context(connection, tenant_id)
    row = _project_delete_impact(connection, tenant_id)
    journey_count = int(row["journey_count"])
    return ProjectDeletionImpactResponse(
        tenantId=tenant_id,
        projectName=str(row["project_name"]),
        projectStatus=str(row["project_status"]),
        journeyCount=journey_count,
        canDelete=journey_count == 0,
        rule="Project hard delete is permitted only when Journey count is zero.",
        cleanupTargets=[
            "Project-owned Document Intelligence data and configuration",
            "Security Tenant",
            "Audit Core Project and setup data",
            "Administrative deletion receipt retained",
        ],
    )


def _di_base_url() -> str:
    value = os.environ.get("DI_BASE_URL", "").strip()
    if not value:
        raise RuntimeError("DI_BASE_URL is required for UC02 administration")
    return value


def _delete_di_project_data(*, base_url: str, token: str, tenant_id: str) -> dict[str, Any]:
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=_PROJECT_DELETE_TIMEOUT_SECONDS,
    ) as client:
        response = client.delete(
            f"/v1/tenants/{tenant_id}/admin/project-data",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code == 404:
        return {"status": "ALREADY_ABSENT"}
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"DI Project cleanup failed with HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return {"status": "REMOVED"}
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return dict(payload["data"])
    return {"status": "REMOVED"}


def _completed_response(operation) -> ProjectDeleteResponse | None:
    receipt = operation.audit_core_receipt or {}
    response = receipt.get("projectDeleteResponse")
    if operation.status == "COMPLETED" and isinstance(response, dict):
        return ProjectDeleteResponse.model_validate(response)
    return None


@router.delete("/project", response_model=ProjectDeleteResponse)
def hard_delete_project(
    tenant_id: str,
    payload: ProjectDeleteConfirmation,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> ProjectDeleteResponse:
    operation = claim_administrative_operation(
        engine,
        operation_type="PROJECT_DELETE",
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        semantic_payload={
            "tenantId": tenant_id,
            "confirmProjectName": payload.confirmProjectName.strip(),
        },
        safe_request_summary={"tenantId": tenant_id, "action": "HARD_DELETE_PROJECT"},
        initiated_by_user_id=admin_request.user_id,
        correlation_id=None,
    )
    completed = _completed_response(operation)
    if completed is not None:
        return completed

    connection = engine.connect()
    transaction = connection.begin()
    di_receipt: dict[str, Any] = operation.di_receipt or {}
    security_receipt: dict[str, Any] = operation.security_receipt or {}
    try:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        set_tenant_context(connection, tenant_id)
        # All delete requests for the same Project serialize even when callers use
        # different idempotency keys.
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"project-hard-delete:{tenant_id}"},
        )
        row = connection.execute(
            text(
                """
                SELECT p.project_name, p.project_status,
                       (SELECT count(*) FROM auditcore.journeys j
                        WHERE j.tenant_id=p.tenant_id) AS journey_count
                FROM auditcore.projects p
                WHERE p.tenant_id=:tenant_id
                FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id},
        ).mappings().one_or_none()
        if row is None:
            update_administrative_operation(
                engine,
                operation_id=operation.operation_id,
                status="FAILED",
                current_step="VALIDATE",
                last_error_code="VAC-NF-001",
                last_error_summary="Project not found.",
                completed=True,
            )
            raise NotFoundError(
                error_code="VAC-NF-001",
                title="Project not found",
                detail="Project not found for the requested tenant.",
            )

        project_name = str(row["project_name"])
        project_status = str(row["project_status"])
        journey_count = int(row["journey_count"])
        if payload.confirmProjectName.strip() != project_name:
            update_administrative_operation(
                engine,
                operation_id=operation.operation_id,
                status="FAILED",
                current_step="CONFIRM",
                last_error_code="VAC-VAL-001",
                last_error_summary="Project deletion confirmation did not match Project name.",
                completed=True,
            )
            raise BusinessValidationError(
                detail="Type the exact Project name to confirm permanent deletion."
            )
        if journey_count > 0:
            update_administrative_operation(
                engine,
                operation_id=operation.operation_id,
                status="FAILED",
                current_step="JOURNEY_GATE",
                last_error_code="VAC-CONFLICT-004",
                last_error_summary=f"Project has {journey_count} Journey(s).",
                completed=True,
            )
            raise ConflictError(
                error_code="VAC-CONFLICT-004",
                title="Project cannot be hard-deleted",
                detail=(
                    f"Project has {journey_count} Journey(s). Hard delete is permitted only "
                    "when Journey count is zero."
                ),
            )

        update_administrative_operation(
            engine,
            operation_id=operation.operation_id,
            status="RUNNING",
            current_step="DI_CLEANUP",
        )
        if not di_receipt:
            di_receipt = _delete_di_project_data(
                base_url=_di_base_url(),
                token=admin_request.bearer_token,
                tenant_id=tenant_id,
            )
            update_administrative_operation(
                engine,
                operation_id=operation.operation_id,
                status="RUNNING",
                current_step="SECURITY_DELETE",
                di_receipt=di_receipt,
            )

        if not security_receipt:
            _delete_security_tenant(
                base_url=_security_base_url(),
                token=admin_request.bearer_token,
                tenant_id=tenant_id,
            )
            security_receipt = {"tenantId": tenant_id, "status": "REMOVED"}
            update_administrative_operation(
                engine,
                operation_id=operation.operation_id,
                status="RUNNING",
                current_step="AUDIT_CORE_DELETE",
                security_receipt=security_receipt,
                di_receipt=di_receipt,
            )

        audit_receipt_value = connection.execute(
            text("SELECT auditcore.hard_delete_zero_journey_project(:tenant_id)"),
            {"tenant_id": tenant_id},
        ).scalar_one()
        audit_receipt = (
            dict(audit_receipt_value)
            if isinstance(audit_receipt_value, dict)
            else {"tenantId": tenant_id, "status": "REMOVED"}
        )
        transaction.commit()

        result = ProjectDeleteResponse(
            operationId=operation.operation_id,
            tenantId=tenant_id,
            projectName=project_name,
            projectStatus=project_status,
            journeyCount=0,
            deletionStatus="COMPLETED",
            diReceipt=di_receipt,
            securityReceipt=security_receipt,
            auditCoreReceipt=audit_receipt,
        )
        update_administrative_operation(
            engine,
            operation_id=operation.operation_id,
            status="COMPLETED",
            current_step="COMPLETE",
            security_receipt=security_receipt,
            di_receipt=di_receipt,
            audit_core_receipt={
                **audit_receipt,
                "projectDeleteResponse": result.model_dump(mode="json"),
            },
            completed=True,
        )
        return result
    except (BusinessValidationError, ConflictError, NotFoundError):
        if transaction.is_active:
            transaction.rollback()
        raise
    except Exception as exc:
        if transaction.is_active:
            transaction.rollback()
        update_administrative_operation(
            engine,
            operation_id=operation.operation_id,
            status="RECOVERY_REQUIRED",
            current_step="RECOVERY_REQUIRED",
            security_receipt=security_receipt or None,
            di_receipt=di_receipt or None,
            last_error_code="VAC-SYS-001",
            last_error_summary=f"Project deletion interrupted: {type(exc).__name__}",
        )
        raise DependencyUnavailableError(
            detail=(
                "Project deletion could not be completed cleanly. The operation is safe to retry "
                "with the same Idempotency-Key."
            )
        ) from exc
    finally:
        connection.close()
