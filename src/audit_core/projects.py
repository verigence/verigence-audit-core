from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_project_admin_request,
)
from audit_core.errors import (
    AuditCoreError,
    BusinessValidationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/project", tags=["project"])


class ProjectSegmentResponse(BaseModel):
    segmentId: UUID
    segmentCode: str
    segmentName: str


class ProjectResponse(BaseModel):
    tenantId: str
    projectCode: str
    # The Project Code people use (e.g. JBR-01); projectCode is the Security tenant code.
    businessCode: str | None = None
    projectName: str
    oemId: UUID
    # Legacy compatibility only; new Project onboarding starts at OEM + Segment.
    productCategoryId: UUID | None = None
    segments: list[ProjectSegmentResponse]
    effectiveStartDate: date
    effectiveEndDate: date | None
    timezoneName: str
    regionCode: str | None
    projectStatus: str
    # SALES or SERVICE (2026-11-05): a service audit has no sales price, discount or product masters.
    auditType: str = "SALES"
    versionNo: int
    createdAtUtc: datetime
    updatedAtUtc: datetime


class ProjectPatch(BaseModel):
    projectName: str | None = Field(default=None, min_length=1, max_length=240)
    businessCode: str | None = Field(default=None, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    oemId: UUID | None = None
    segmentIds: list[UUID] | None = None
    effectiveStartDate: date | None = None
    effectiveEndDate: date | None = None
    timezoneName: str | None = Field(default=None, min_length=1, max_length=100)
    regionCode: str | None = Field(default=None, max_length=100)
    # Sales or service audit (product owner, 2026-11-09): a Super Admin sets it, and only while the
    # Project has no Journeys or published masters, since the two kinds use different masters.
    auditType: Literal["SALES", "SERVICE"] | None = None


def _not_found() -> NotFoundError:
    return NotFoundError(
        error_code="VAC-NF-001",
        title="Project not found",
        detail="Project not found for the requested tenant.",
    )


def business_code_conflict(code: str) -> ConflictError:
    return ConflictError(
        error_code="VAC-CONFLICT-002",
        title="Project Code already used",
        detail=f"Project Code {code} is already used by another Project. Choose another code.",
    )


def _project_row(connection: Connection, tenant_id: str):
    row = connection.execute(
        text(
            """
            SELECT tenant_id, project_code, business_code, project_name, oem_id, product_category_id,
                   effective_start_date, effective_end_date, timezone_name, region_code,
                   project_status, audit_type, version_no, created_at_utc, updated_at_utc
            FROM auditcore.projects
            WHERE tenant_id = :tenant_id
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        raise _not_found()
    return row


def _segment_rows(connection: Connection, tenant_id: str):
    return connection.execute(
        text(
            """
            SELECT s.segment_id, s.segment_code, s.segment_name
            FROM auditcore.project_segments ps
            JOIN auditcore.segments s ON s.segment_id = ps.segment_id
            WHERE ps.tenant_id=:tenant_id
            ORDER BY s.segment_name, s.segment_code
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().all()


def _project_response(connection: Connection, row) -> ProjectResponse:
    return ProjectResponse(
        tenantId=row["tenant_id"],
        projectCode=row["project_code"],
        businessCode=row.get("business_code"),
        projectName=row["project_name"],
        oemId=row["oem_id"],
        productCategoryId=row["product_category_id"],
        segments=[
            ProjectSegmentResponse(
                segmentId=segment["segment_id"],
                segmentCode=segment["segment_code"],
                segmentName=segment["segment_name"],
            )
            for segment in _segment_rows(connection, row["tenant_id"])
        ],
        effectiveStartDate=row["effective_start_date"],
        effectiveEndDate=row["effective_end_date"],
        timezoneName=row["timezone_name"],
        regionCode=row["region_code"],
        projectStatus=row["project_status"],
        auditType=row.get("audit_type") or "SALES",
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
        raise ValidationError(
            detail="If-Match must contain the current positive version number."
        ) from exc
    if version <= 0:
        raise ValidationError(
            detail="If-Match must contain the current positive version number."
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
                        SELECT 1 FROM auditcore.project_product_master_versions
                        WHERE tenant_id = :tenant_id AND lifecycle_status = 'PUBLISHED'
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
                        SELECT 1 FROM auditcore.discount_policy_versions
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
        raise BusinessValidationError(
            detail=f"{label} does not reference an existing approved value."
        )


def _validate_segment_ids(
    connection: Connection,
    *,
    oem_id: UUID,
    segment_ids: list[UUID],
) -> None:
    if len(set(segment_ids)) != len(segment_ids):
        raise BusinessValidationError(detail="Project Segments cannot contain duplicates.")
    configured = set(
        connection.execute(
            text(
                """
                SELECT segment_id FROM auditcore.segments
                WHERE is_active=true
                """
            ),
            {"oem_id": oem_id},
        ).scalars().all()
    )
    selected = set(segment_ids)
    if configured and not selected:
        raise BusinessValidationError(
            detail="Select at least one active Segment."
        )
    if not selected.issubset(configured):
        raise BusinessValidationError(
            detail="Every selected Segment must reference an active universal Segment."
        )


def _replace_segments(
    connection: Connection,
    *,
    tenant_id: str,
    segment_ids: list[UUID],
    actor_id: str,
) -> None:
    connection.execute(
        text("DELETE FROM auditcore.project_segments WHERE tenant_id=:tenant_id"),
        {"tenant_id": tenant_id},
    )
    for segment_id in segment_ids:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.project_segments (
                    tenant_id, segment_id, created_by_actor_id
                ) VALUES (:tenant_id, :segment_id, :actor_id)
                """
            ),
            {"tenant_id": tenant_id, "segment_id": segment_id, "actor_id": actor_id},
        )


@router.get("", response_model=ProjectResponse)
def get_project(
    tenant_id: str,
    response: Response,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectResponse:
    del admin_request
    set_tenant_context(connection, tenant_id)
    row = _project_row(connection, tenant_id)
    _set_etag(response, row["version_no"])
    return _project_response(connection, row)


@router.patch("", response_model=ProjectResponse)
def patch_project(
    tenant_id: str,
    patch: ProjectPatch,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ProjectResponse:
    set_tenant_context(connection, tenant_id)

    supplied = patch.model_fields_set
    if not supplied:
        raise ValidationError(detail="At least one Project field must be supplied.")

    non_nullable = {
        "projectName": patch.projectName,
        "oemId": patch.oemId,
        "segmentIds": patch.segmentIds,
        "effectiveStartDate": patch.effectiveStartDate,
        "timezoneName": patch.timezoneName,
        "auditType": patch.auditType,
    }
    if any(name in supplied and value is None for name, value in non_nullable.items()):
        raise ValidationError(detail="Required Project fields cannot be set to null.")

    current = _project_row(connection, tenant_id)
    expected_version = _parse_if_match(if_match)
    if expected_version != current["version_no"]:
        raise ConflictError(
            error_code="VAC-CONFLICT-001",
            title="Version conflict",
            detail="Project was changed by another request. Refresh and retry.",
        )

    next_oem_id = patch.oemId if "oemId" in supplied and patch.oemId is not None else current["oem_id"]
    if "oemId" in supplied and patch.oemId is not None:
        _require_reference_exists(
            connection,
            table="oems",
            id_column="oem_id",
            value=patch.oemId,
            label="OEM",
        )

    current_segment_ids = [row["segment_id"] for row in _segment_rows(connection, tenant_id)]
    next_segment_ids = (
        patch.segmentIds
        if "segmentIds" in supplied and patch.segmentIds is not None
        else current_segment_ids
    )
    if "oemId" in supplied or "segmentIds" in supplied:
        _validate_segment_ids(connection, oem_id=next_oem_id, segment_ids=next_segment_ids)

    restricted_changed = (
        ("oemId" in supplied and patch.oemId != current["oem_id"])
        or ("segmentIds" in supplied and set(next_segment_ids) != set(current_segment_ids))
        or (
            "effectiveStartDate" in supplied
            and patch.effectiveStartDate != current["effective_start_date"]
        )
    )
    if "auditType" in supplied and patch.auditType != current["audit_type"]:
        if not admin_request.admin_context.is_super_admin:
            raise AuditCoreError(
                error_code="VAC-AUTH-AUDIT-TYPE",
                status_code=403,
                title="Not permitted",
                detail="Only a Super Admin can change whether a Project is a sales or a service audit.",
            )
        # Every Project starts with the same default masters, so only operational records lock it.
        if connection.execute(
            text("SELECT EXISTS (SELECT 1 FROM auditcore.journeys WHERE tenant_id = :tenant_id)"),
            {"tenant_id": tenant_id},
        ).scalar_one():
            raise BusinessValidationError(
                detail="The audit type cannot be changed after Journeys have been recorded for the Project."
            )
        if current["audit_type"] == "SERVICE" and connection.execute(
            text("SELECT EXISTS (SELECT 1 FROM auditcore.sevasutra_instances WHERE tenant_id = :tenant_id)"),
            {"tenant_id": tenant_id},
        ).scalar_one():
            raise BusinessValidationError(
                detail="This Project has a SevaSutra instance, so it stays a service audit."
            )

    if restricted_changed and _has_project_dependencies(connection, tenant_id):
        raise BusinessValidationError(
            detail=(
                "OEM, Segments and Effective Start Date cannot be changed after operational "
                "Journeys or dependent published masters exist."
            )
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
        raise BusinessValidationError(
            detail="Effective End Date cannot be earlier than Effective Start Date."
        )

    columns = {
        "projectName": ("project_name", patch.projectName),
        "businessCode": ("business_code", patch.businessCode),
        "oemId": ("oem_id", patch.oemId),
        "effectiveStartDate": ("effective_start_date", patch.effectiveStartDate),
        "effectiveEndDate": ("effective_end_date", patch.effectiveEndDate),
        "timezoneName": ("timezone_name", patch.timezoneName),
        "regionCode": ("region_code", patch.regionCode),
        "auditType": ("audit_type", patch.auditType),
    }
    assignments: list[str] = []
    parameters: dict[str, object] = {
        "tenant_id": tenant_id,
        "expected_version": expected_version,
        "actor_id": admin_request.user_id,
    }
    for field_name in supplied:
        if field_name == "segmentIds":
            continue
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
    # Project Codes are unique across Projects (ux_projects_business_code);
    # other Projects are outside this tenant's view, so the index decides.
    try:
        with connection.begin_nested():
            row = connection.execute(
                text(
                    f"""
                    UPDATE auditcore.projects
                    SET {', '.join(assignments)}
                    WHERE tenant_id = :tenant_id AND version_no = :expected_version
                    RETURNING tenant_id, project_code, business_code, project_name, oem_id, product_category_id,
                              effective_start_date, effective_end_date, timezone_name, region_code,
                              project_status, audit_type, version_no, created_at_utc, updated_at_utc
                    """
                ),
                parameters,
            ).mappings().one_or_none()
    except IntegrityError as exc:
        if "ux_projects_business_code" in str(exc.orig):
            raise business_code_conflict(str(patch.businessCode)) from exc
        raise
    if row is None:
        raise ConflictError(
            error_code="VAC-CONFLICT-001",
            title="Version conflict",
            detail="Project was changed by another request. Refresh and retry.",
        )

    if "segmentIds" in supplied:
        _replace_segments(
            connection,
            tenant_id=tenant_id,
            segment_ids=next_segment_ids,
            actor_id=admin_request.user_id,
        )

    _set_etag(response, row["version_no"])
    return _project_response(connection, row)
