from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.authorization import require_tenant
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import AuditCoreError, ConflictError, NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}/project", tags=["project"])


class ProjectResponse(BaseModel):
    tenantId: str
    projectCode: str
    projectName: str
    oemId: UUID
    productCategoryId: UUID
    effectiveStartDate: date
    effectiveEndDate: date | None
    timezoneName: str
    regionCode: str | None
    projectStatus: str
    versionNo: int
    createdAtUtc: datetime
    updatedAtUtc: datetime


class ProjectPatch(BaseModel):
    projectName: str | None = Field(default=None, min_length=1, max_length=240)
    oemId: UUID | None = None
    productCategoryId: UUID | None = None
    effectiveStartDate: date | None = None
    effectiveEndDate: date | None = None
    timezoneName: str | None = Field(default=None, min_length=1, max_length=100)
    regionCode: str | None = Field(default=None, max_length=100)


def _not_found() -> NotFoundError:
    return NotFoundError(
        error_code="VAC-NF-001",
        title="Project not found",
        detail="Project not found for the requested tenant.",
    )


def _validation(detail: str) -> AuditCoreError:
    return AuditCoreError(
        error_code="VAC-VAL-002",
        status_code=422,
        title="Business validation failed",
        detail=detail,
    )


def _project_row(connection: Connection, tenant_id: str):
    row = connection.execute(
        text(
            """
            SELECT tenant_id, project_code, project_name, oem_id, product_category_id,
                   effective_start_date, effective_end_date, timezone_name, region_code,
                   project_status, version_no, created_at_utc, updated_at_utc
            FROM auditcore.projects
            WHERE tenant_id = :tenant_id
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        raise _not_found()
    return row


def _project_response(row) -> ProjectResponse:
    return ProjectResponse(
        tenantId=row["tenant_id"],
        projectCode=row["project_code"],
        projectName=row["project_name"],
        oemId=row["oem_id"],
        productCategoryId=row["product_category_id"],
        effectiveStartDate=row["effective_start_date"],
        effectiveEndDate=row["effective_end_date"],
        timezoneName=row["timezone_name"],
        regionCode=row["region_code"],
        projectStatus=row["project_status"],
        versionNo=row["version_no"],
        createdAtUtc=row["created_at_utc"],
        updatedAtUtc=row["updated_at_utc"],
    )


def _set_etag(response: Response, version_no: int) -> None:
    response.headers["ETag"] = f'"{version_no}"'


def _parse_if_match(value: str) -> int:
    candidate = value.strip()
    if candidate.startswith('"') and candidate.endswith('"') and len(candidate) >= 2:
        candidate = candidate[1:-1]
    try:
        version = int(candidate)
    except ValueError as exc:
        raise AuditCoreError(
            error_code="VAC-VAL-001",
            status_code=400,
            title="Validation failed",
            detail="If-Match must contain the current positive version number.",
        ) from exc
    if version <= 0:
        raise AuditCoreError(
            error_code="VAC-VAL-001",
            status_code=400,
            title="Validation failed",
            detail="If-Match must contain the current positive version number.",
        )
    return version


def _has_project_dependencies(connection: Connection, tenant_id: str) -> bool:
    return bool(
        connection.execute(
            text(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM auditcore.journeys
                        WHERE tenant_id = :tenant_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM auditcore.project_policy_versions
                        WHERE tenant_id = :tenant_id AND lifecycle_status = 'PUBLISHED'
                    )
                    OR EXISTS (
                        SELECT 1 FROM auditcore.price_list_versions
                        WHERE tenant_id = :tenant_id AND lifecycle_status = 'PUBLISHED'
                    )
                    OR EXISTS (
                        SELECT 1 FROM auditcore.discount_scheme_versions
                        WHERE tenant_id = :tenant_id AND lifecycle_status = 'PUBLISHED'
                    )
                    OR EXISTS (
                        SELECT 1 FROM auditcore.document_requirement_profile_versions
                        WHERE tenant_id = :tenant_id AND lifecycle_status = 'PUBLISHED'
                    )
                    OR EXISTS (
                        SELECT 1 FROM auditcore.audit_control_versions
                        WHERE tenant_id = :tenant_id AND lifecycle_status = 'PUBLISHED'
                    )
                """
            ),
            {"tenant_id": tenant_id},
        ).scalar_one()
    )


def _require_reference_exists(
    connection: Connection,
    *,
    table: str,
    id_column: str,
    value: UUID,
    label: str,
) -> None:
    exists = connection.execute(
        text(f"SELECT 1 FROM auditcore.{table} WHERE {id_column} = :value"),
        {"value": value},
    ).scalar_one_or_none()
    if exists is None:
        raise _validation(f"{label} does not reference an existing approved value.")


@router.get("", response_model=ProjectResponse)
def get_project(
    tenant_id: str,
    response: Response,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectResponse:
    require_tenant(principal, tenant_id)
    set_tenant_context(connection, tenant_id)
    row = _project_row(connection, tenant_id)
    _set_etag(response, row["version_no"])
    return _project_response(row)


@router.patch("", response_model=ProjectResponse)
def patch_project(
    tenant_id: str,
    patch: ProjectPatch,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1)],
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectResponse:
    require_tenant(principal, tenant_id)
    set_tenant_context(connection, tenant_id)

    supplied = patch.model_fields_set
    if not supplied:
        raise AuditCoreError(
            error_code="VAC-VAL-001",
            status_code=400,
            title="Validation failed",
            detail="At least one Project field must be supplied.",
        )

    non_nullable = {
        "projectName": patch.projectName,
        "oemId": patch.oemId,
        "productCategoryId": patch.productCategoryId,
        "effectiveStartDate": patch.effectiveStartDate,
        "timezoneName": patch.timezoneName,
    }
    if any(name in supplied and value is None for name, value in non_nullable.items()):
        raise AuditCoreError(
            error_code="VAC-VAL-001",
            status_code=400,
            title="Validation failed",
            detail="Required Project fields cannot be set to null.",
        )

    current = _project_row(connection, tenant_id)
    expected_version = _parse_if_match(if_match)
    if expected_version != current["version_no"]:
        raise ConflictError(
            error_code="VAC-CONFLICT-001",
            title="Version conflict",
            detail="Project was changed by another request. Refresh and retry.",
        )

    if "oemId" in supplied and patch.oemId is not None:
        _require_reference_exists(
            connection,
            table="oems",
            id_column="oem_id",
            value=patch.oemId,
            label="OEM",
        )
    if "productCategoryId" in supplied and patch.productCategoryId is not None:
        _require_reference_exists(
            connection,
            table="product_categories",
            id_column="product_category_id",
            value=patch.productCategoryId,
            label="Product Category",
        )

    restricted_changed = (
        ("oemId" in supplied and patch.oemId != current["oem_id"])
        or (
            "productCategoryId" in supplied
            and patch.productCategoryId != current["product_category_id"]
        )
        or (
            "effectiveStartDate" in supplied
            and patch.effectiveStartDate != current["effective_start_date"]
        )
    )
    if restricted_changed and _has_project_dependencies(connection, tenant_id):
        raise _validation(
            "OEM, Product Category and Effective Start Date cannot be changed after "
            "operational Journeys or dependent published masters exist."
        )

    effective_start = (
        patch.effectiveStartDate
        if "effectiveStartDate" in supplied
        else current["effective_start_date"]
    )
    effective_end = (
        patch.effectiveEndDate if "effectiveEndDate" in supplied else current["effective_end_date"]
    )
    if effective_end is not None and effective_end < effective_start:
        raise _validation("Effective End Date cannot be earlier than Effective Start Date.")

    columns = {
        "projectName": ("project_name", patch.projectName),
        "oemId": ("oem_id", patch.oemId),
        "productCategoryId": ("product_category_id", patch.productCategoryId),
        "effectiveStartDate": ("effective_start_date", patch.effectiveStartDate),
        "effectiveEndDate": ("effective_end_date", patch.effectiveEndDate),
        "timezoneName": ("timezone_name", patch.timezoneName),
        "regionCode": ("region_code", patch.regionCode),
    }
    assignments: list[str] = []
    parameters: dict[str, object] = {
        "tenant_id": tenant_id,
        "expected_version": expected_version,
        "actor_id": principal.subject,
    }
    for field_name in supplied:
        column, value = columns[field_name]
        parameter_name = f"value_{field_name}"
        assignments.append(f"{column} = :{parameter_name}")
        parameters[parameter_name] = value

    assignments.extend(
        [
            "updated_by_actor_id = :actor_id",
            "updated_at_utc = now()",
            "version_no = version_no + 1",
        ]
    )
    row = connection.execute(
        text(
            f"""
            UPDATE auditcore.projects
            SET {', '.join(assignments)}
            WHERE tenant_id = :tenant_id AND version_no = :expected_version
            RETURNING tenant_id, project_code, project_name, oem_id, product_category_id,
                      effective_start_date, effective_end_date, timezone_name, region_code,
                      project_status, version_no, created_at_utc, updated_at_utc
            """
        ),
        parameters,
    ).mappings().one_or_none()
    if row is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-001",
            title="Version conflict",
            detail="Project was changed by another request. Refresh and retry.",
        )

    _set_etag(response, row["version_no"])
    return _project_response(row)
