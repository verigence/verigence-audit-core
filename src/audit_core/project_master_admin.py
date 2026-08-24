from __future__ import annotations

import hashlib
import json
from typing import Annotated
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import HumanAdminRequest, get_connection, require_project_admin_request
from audit_core.errors import ConflictError, NotFoundError

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/project-masters",
    tags=["project-master-admin"],
)

_MASTER_RESET_TABLES = (
    "project_master_import_rows",
    "project_master_imports",
    "project_product_master_items",
    "project_product_master_versions",
    "project_product_masters",
    "price_list_items",
    "price_list_versions",
    "price_lists",
    "discount_policy_parameters",
    "discount_policy_versions",
    "audit_control_versions",
    "audit_controls",
    "document_requirement_items",
    "document_requirement_profile_versions",
    "document_requirement_profiles",
    "project_policy_versions",
)


class ProjectMasterDeleteResponse(BaseModel):
    tenantId: str
    action: str
    status: str
    deletedRows: dict[str, int]


def _require_configuring(connection: Connection, tenant_id: str) -> None:
    status = connection.execute(
        text(
            "SELECT project_status FROM auditcore.projects "
            "WHERE tenant_id=:tenant_id"
        ),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    if status is None:
        raise NotFoundError(
            error_code="VAC-NF-001",
            title="Project not found",
            detail="Project not found for the requested Tenant.",
        )
    if status != "CONFIGURING":
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Project Masters cannot be deleted",
            detail="Project Masters can be reset or deleted only while the Project is CONFIGURING.",
        )


def _record_admin_operation(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    operation_type: str,
    summary: dict[str, object],
    receipt: dict[str, object],
) -> None:
    operation_id = str(uuid4())
    idempotency_key = f"{operation_type.lower()}-{operation_id}"
    semantic_hash = hashlib.sha256(
        json.dumps(summary, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.administrative_operations (
                operation_id, operation_type, tenant_id, idempotency_key,
                semantic_request_hash, status, current_step, initiated_by_user_id,
                safe_request_summary, audit_core_receipt, completed_at_utc
            ) VALUES (
                :operation_id, :operation_type, :tenant_id, :idempotency_key,
                :semantic_hash, 'COMPLETED', 'COMPLETE', :actor_id,
                CAST(:summary AS jsonb), CAST(:receipt AS jsonb), now()
            )
            """
        ),
        {
            "operation_id": operation_id,
            "operation_type": operation_type,
            "tenant_id": tenant_id,
            "idempotency_key": idempotency_key,
            "semantic_hash": semantic_hash,
            "actor_id": actor_id,
            "summary": json.dumps(summary, default=str),
            "receipt": json.dumps(receipt, default=str),
        },
    )


def _delete(connection: Connection, table: str, where: str, params: dict[str, object]) -> int:
    result = connection.execute(
        text(f"DELETE FROM auditcore.{table} WHERE {where}"),
        params,
    )
    return int(result.rowcount or 0)


def _delete_import_slot(
    connection: Connection,
    *,
    tenant_id: str,
    master_key: str,
    segment_id: UUID | None,
) -> dict[str, int]:
    params: dict[str, object] = {
        "tenant_id": tenant_id,
        "master_key": master_key,
    }
    segment_clause = "segment_id IS NULL"
    if segment_id is not None:
        params["segment_id"] = segment_id
        segment_clause = "segment_id=:segment_id"

    imports = connection.execute(
        text(
            f"""
            SELECT import_id, confirmation_receipt
            FROM auditcore.project_master_imports
            WHERE tenant_id=:tenant_id
              AND master_key=:master_key
              AND {segment_clause}
            ORDER BY created_at_utc
            """
        ),
        params,
    ).mappings().all()

    import_ids = [row["import_id"] for row in imports]
    product_version_ids: set[UUID] = set()
    price_version_ids: set[UUID] = set()
    discount_version_ids: set[UUID] = set()
    for row in imports:
        receipt = dict(row["confirmation_receipt"] or {})
        if receipt.get("productMasterVersionId"):
            product_version_ids.add(UUID(str(receipt["productMasterVersionId"])))
        if receipt.get("priceListVersionId"):
            price_version_ids.add(UUID(str(receipt["priceListVersionId"])))
        if receipt.get("discountPolicyVersionId"):
            discount_version_ids.add(UUID(str(receipt["discountPolicyVersionId"])))

    deleted: dict[str, int] = {}
    for version_id in price_version_ids:
        version_params = {"tenant_id": tenant_id, "version_id": version_id}
        deleted["price_list_items"] = deleted.get("price_list_items", 0) + _delete(
            connection,
            "price_list_items",
            "tenant_id=:tenant_id AND price_list_version_id=:version_id",
            version_params,
        )
        deleted["price_list_versions"] = deleted.get("price_list_versions", 0) + _delete(
            connection,
            "price_list_versions",
            "tenant_id=:tenant_id AND price_list_version_id=:version_id",
            version_params,
        )

    for version_id in product_version_ids:
        version_params = {"tenant_id": tenant_id, "version_id": version_id}
        deleted["project_product_master_items"] = deleted.get(
            "project_product_master_items", 0
        ) + _delete(
            connection,
            "project_product_master_items",
            "tenant_id=:tenant_id AND version_id=:version_id",
            version_params,
        )
        deleted["project_product_master_versions"] = deleted.get(
            "project_product_master_versions", 0
        ) + _delete(
            connection,
            "project_product_master_versions",
            "tenant_id=:tenant_id AND version_id=:version_id",
            version_params,
        )

    for version_id in discount_version_ids:
        version_params = {"tenant_id": tenant_id, "version_id": version_id}
        deleted["discount_policy_parameters"] = deleted.get(
            "discount_policy_parameters", 0
        ) + _delete(
            connection,
            "discount_policy_parameters",
            "tenant_id=:tenant_id AND discount_policy_version_id=:version_id",
            version_params,
        )
        deleted["discount_policy_versions"] = deleted.get(
            "discount_policy_versions", 0
        ) + _delete(
            connection,
            "discount_policy_versions",
            "tenant_id=:tenant_id AND discount_policy_version_id=:version_id",
            version_params,
        )

    for import_id in import_ids:
        import_params = {"tenant_id": tenant_id, "import_id": import_id}
        deleted["project_master_import_rows"] = deleted.get(
            "project_master_import_rows", 0
        ) + _delete(
            connection,
            "project_master_import_rows",
            "tenant_id=:tenant_id AND import_id=:import_id",
            import_params,
        )
        deleted["project_master_imports"] = deleted.get(
            "project_master_imports", 0
        ) + _delete(
            connection,
            "project_master_imports",
            "tenant_id=:tenant_id AND import_id=:import_id",
            import_params,
        )
    return {key: value for key, value in deleted.items() if value}


@router.post("/reset", response_model=ProjectMasterDeleteResponse)
def reset_project_masters(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectMasterDeleteResponse:
    set_tenant_context(connection, tenant_id)
    _require_configuring(connection, tenant_id)
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"master-reset:{tenant_id}"},
    )

    deleted: dict[str, int] = {}
    for table in _MASTER_RESET_TABLES:
        count = _delete(
            connection,
            table,
            "tenant_id=:tenant_id",
            {"tenant_id": tenant_id},
        )
        if count:
            deleted[table] = count

    _record_admin_operation(
        connection,
        tenant_id=tenant_id,
        actor_id=admin_request.user_id,
        operation_type="PROJECT_MASTERS_RESET",
        summary={"action": "RESET_PROJECT_MASTERS", "tenantId": tenant_id},
        receipt={"deletedRows": deleted},
    )
    logger.info(
        "project_masters_reset",
        tenant_id=tenant_id,
        actor_user_id=admin_request.user_id,
        deleted_rows=deleted,
    )
    return ProjectMasterDeleteResponse(
        tenantId=tenant_id,
        action="RESET_PROJECT_MASTERS",
        status="COMPLETED",
        deletedRows=deleted,
    )


@router.delete(
    "/mahindra/segments/{segment_id}",
    response_model=ProjectMasterDeleteResponse,
)
def delete_mahindra_segment_master(
    tenant_id: str,
    segment_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectMasterDeleteResponse:
    set_tenant_context(connection, tenant_id)
    _require_configuring(connection, tenant_id)
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"master-delete:{tenant_id}:segment:{segment_id}"},
    )
    deleted = _delete_import_slot(
        connection,
        tenant_id=tenant_id,
        master_key="MAHINDRA_SEGMENT_MASTER",
        segment_id=segment_id,
    )
    _record_admin_operation(
        connection,
        tenant_id=tenant_id,
        actor_id=admin_request.user_id,
        operation_type="PROJECT_MASTER_DELETE",
        summary={
            "action": "DELETE_MAHINDRA_SEGMENT_MASTER",
            "tenantId": tenant_id,
            "segmentId": str(segment_id),
        },
        receipt={"deletedRows": deleted},
    )
    logger.info(
        "project_master_deleted",
        tenant_id=tenant_id,
        segment_id=str(segment_id),
        actor_user_id=admin_request.user_id,
        deleted_rows=deleted,
    )
    return ProjectMasterDeleteResponse(
        tenantId=tenant_id,
        action="DELETE_MAHINDRA_SEGMENT_MASTER",
        status="COMPLETED",
        deletedRows=deleted,
    )


@router.delete(
    "/mahindra/discount-policy",
    response_model=ProjectMasterDeleteResponse,
)
def delete_mahindra_discount_policy(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectMasterDeleteResponse:
    set_tenant_context(connection, tenant_id)
    _require_configuring(connection, tenant_id)
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"master-delete:{tenant_id}:discount-policy"},
    )
    deleted = _delete_import_slot(
        connection,
        tenant_id=tenant_id,
        master_key="DISCOUNT_POLICY",
        segment_id=None,
    )
    _record_admin_operation(
        connection,
        tenant_id=tenant_id,
        actor_id=admin_request.user_id,
        operation_type="PROJECT_MASTER_DELETE",
        summary={"action": "DELETE_DISCOUNT_POLICY", "tenantId": tenant_id},
        receipt={"deletedRows": deleted},
    )
    logger.info(
        "project_master_deleted",
        tenant_id=tenant_id,
        master_key="DISCOUNT_POLICY",
        actor_user_id=admin_request.user_id,
        deleted_rows=deleted,
    )
    return ProjectMasterDeleteResponse(
        tenantId=tenant_id,
        action="DELETE_DISCOUNT_POLICY",
        status="COMPLETED",
        deletedRows=deleted,
    )
