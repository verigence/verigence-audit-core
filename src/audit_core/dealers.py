from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.errors import (
    BusinessValidationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from audit_core.idempotency import execute_idempotent_json_command

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["dealers"])


class DealerCreate(BaseModel):
    dealerName: str = Field(min_length=1, max_length=240)
    legalName: str | None = Field(default=None, max_length=240)


class DealerPatch(BaseModel):
    dealerName: str | None = Field(default=None, min_length=1, max_length=240)
    legalName: str | None = Field(default=None, max_length=240)
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class DealerResponse(BaseModel):
    dealerId: UUID
    dealerCode: str
    dealerName: str
    legalName: str | None
    status: str
    versionNo: int


class OutletCreate(BaseModel):
    outletName: str = Field(min_length=1, max_length=240)
    outletClassification: Literal["ONSITE", "SATELLITE"] = "ONSITE"
    addressText: str | None = None
    city: str | None = Field(default=None, max_length=160)
    stateRegion: str | None = Field(default=None, max_length=160)
    postalCode: str | None = Field(default=None, max_length=40)
    googlePlaceId: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    monthlyVehicleVolume: int | None = Field(default=None, ge=0)


class OutletPatch(BaseModel):
    outletName: str | None = Field(default=None, min_length=1, max_length=240)
    outletClassification: Literal["ONSITE", "SATELLITE"] | None = None
    addressText: str | None = None
    city: str | None = Field(default=None, max_length=160)
    stateRegion: str | None = Field(default=None, max_length=160)
    postalCode: str | None = Field(default=None, max_length=40)
    googlePlaceId: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    monthlyVehicleVolume: int | None = Field(default=None, ge=0)
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class OutletResponse(BaseModel):
    outletId: UUID
    dealerId: UUID
    outletCode: str
    outletName: str
    outletClassification: str
    addressText: str | None
    city: str | None
    stateRegion: str | None
    postalCode: str | None
    googlePlaceId: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    monthlyVehicleVolume: int | None
    status: str
    versionNo: int


class DeletionImpactResponse(BaseModel):
    canDelete: bool
    dependencies: dict[str, int]


def _not_found(resource: str) -> NotFoundError:
    return NotFoundError(
        error_code="VAC-NF-002" if resource == "Dealer" else "VAC-NF-003",
        title=f"{resource} not found",
        detail=f"{resource} not found for the requested tenant hierarchy.",
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


def _dealer_response(row) -> DealerResponse:
    return DealerResponse(
        dealerId=row["dealer_id"],
        dealerCode=row["dealer_code"],
        dealerName=row["dealer_name"],
        legalName=row["legal_name"],
        status=row["status"],
        versionNo=row["version_no"],
    )


def _outlet_response(row) -> OutletResponse:
    return OutletResponse(
        outletId=row["outlet_id"],
        dealerId=row["dealer_id"],
        outletCode=row["outlet_code"],
        outletName=row["outlet_name"],
        outletClassification=row["outlet_classification"],
        addressText=row["address_text"],
        city=row["city"],
        stateRegion=row["state_region"],
        postalCode=row["postal_code"],
        googlePlaceId=row["google_place_id"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        monthlyVehicleVolume=row["monthly_vehicle_volume"],
        status=row["status"],
        versionNo=row["version_no"],
    )


def _dealer_exists(connection: Connection, tenant_id: str, dealer_id: UUID) -> bool:
    return (
        connection.execute(
            text(
                "SELECT 1 FROM auditcore.dealers "
                "WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id"
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id},
        ).scalar_one_or_none()
        is not None
    )


def _outlet_exists(
    connection: Connection,
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
) -> bool:
    return (
        connection.execute(
            text(
                """
                SELECT 1 FROM auditcore.dealer_outlets
                WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
                  AND outlet_id = :outlet_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
            },
        ).scalar_one_or_none()
        is not None
    )


def _dealer_impact(connection: Connection, tenant_id: str, dealer_id: UUID) -> dict[str, int]:
    row = connection.execute(
        text(
            """
            SELECT
                (SELECT count(*) FROM auditcore.dealer_outlets
                 WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id) AS outlets,
                (SELECT count(*) FROM auditcore.business_assignments
                 WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id) AS business_assignments,
                (SELECT count(*) FROM auditcore.discount_scheme_eligibility
                 WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id) AS discount_eligibility,
                (SELECT count(*) FROM auditcore.workflow_tasks
                 WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id) AS workflow_tasks
            """
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id},
    ).mappings().one()
    return {
        "outlets": row["outlets"],
        "businessAssignments": row["business_assignments"],
        "discountEligibility": row["discount_eligibility"],
        "workflowTasks": row["workflow_tasks"],
    }


def _outlet_impact(
    connection: Connection,
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
) -> dict[str, int]:
    row = connection.execute(
        text(
            """
            SELECT
                (SELECT count(*) FROM auditcore.dealership_staff
                 WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
                   AND outlet_id = :outlet_id) AS dealership_staff,
                (SELECT count(*) FROM auditcore.business_assignments
                 WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
                   AND outlet_id = :outlet_id) AS business_assignments,
                (SELECT count(*) FROM auditcore.discount_scheme_eligibility
                 WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
                   AND outlet_id = :outlet_id) AS discount_eligibility,
                (SELECT count(*) FROM auditcore.customers
                 WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
                   AND outlet_id = :outlet_id) AS customers,
                (SELECT count(*) FROM auditcore.journeys
                 WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
                   AND outlet_id = :outlet_id) AS journeys,
                (SELECT count(*) FROM auditcore.workflow_tasks
                 WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
                   AND outlet_id = :outlet_id) AS workflow_tasks,
                (SELECT count(*) FROM auditcore.daily_ops_runs
                 WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id) AS daily_ops_runs,
                (SELECT count(*) FROM auditcore.activity_records
                 WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id) AS activity_records,
                (SELECT count(*) FROM auditcore.pc_daily_notes
                 WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id) AS pc_daily_notes
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
        },
    ).mappings().one()
    return {
        "dealershipStaff": row["dealership_staff"],
        "businessAssignments": row["business_assignments"],
        "discountEligibility": row["discount_eligibility"],
        "customers": row["customers"],
        "journeys": row["journeys"],
        "workflowTasks": row["workflow_tasks"],
        "dailyOpsRuns": row["daily_ops_runs"],
        "activityRecords": row["activity_records"],
        "pcDailyNotes": row["pc_daily_notes"],
    }


def _can_delete(dependencies: dict[str, int]) -> bool:
    return not any(dependencies.values())


@router.post("/dealers", response_model=DealerResponse, status_code=status.HTTP_201_CREATED)
def create_dealer(
    tenant_id: str,
    payload: DealerCreate,
    response: Response,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DealerResponse:
    set_tenant_context(connection, tenant_id)
    dealer_id = uuid4()
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.dealers (
                tenant_id, dealer_id, dealer_code, dealer_name, legal_name,
                created_by_actor_id
            ) VALUES (
                :tenant_id, :dealer_id, :dealer_code, :dealer_name, :legal_name,
                :actor_id
            )
            RETURNING dealer_id, dealer_code, dealer_name, legal_name, status, version_no
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "dealer_code": dealer_id.hex,
            "dealer_name": payload.dealerName,
            "legal_name": payload.legalName,
            "actor_id": admin_request.user_id,
        },
    ).mappings().one()
    _set_etag(response, row["version_no"])
    return _dealer_response(row)


@router.get("/dealers", response_model=list[DealerResponse])
def list_dealers(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[DealerResponse]:
    set_tenant_context(connection, tenant_id)
    rows = connection.execute(
        text(
            """
            SELECT dealer_id, dealer_code, dealer_name, legal_name, status, version_no
            FROM auditcore.dealers
            WHERE tenant_id = :tenant_id
            ORDER BY dealer_code
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings()
    return [_dealer_response(row) for row in rows]


@router.get("/dealers/{dealer_id}", response_model=DealerResponse)
def get_dealer(
    tenant_id: str,
    dealer_id: UUID,
    response: Response,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DealerResponse:
    set_tenant_context(connection, tenant_id)
    row = connection.execute(
        text(
            """
            SELECT dealer_id, dealer_code, dealer_name, legal_name, status, version_no
            FROM auditcore.dealers
            WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
            """
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id},
    ).mappings().one_or_none()
    if row is None:
        raise _not_found("Dealer")
    _set_etag(response, row["version_no"])
    return _dealer_response(row)


@router.patch("/dealers/{dealer_id}", response_model=DealerResponse)
def patch_dealer(
    tenant_id: str,
    dealer_id: UUID,
    payload: DealerPatch,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DealerResponse:
    set_tenant_context(connection, tenant_id)
    supplied = payload.model_fields_set
    if not supplied:
        raise ValidationError(detail="At least one Dealer field must be supplied.")
    if "dealerName" in supplied and payload.dealerName is None:
        raise ValidationError(detail="Dealer Name cannot be set to null.")
    if "status" in supplied and payload.status is None:
        raise ValidationError(detail="Dealer status cannot be set to null.")

    expected_version = _parse_if_match(if_match)
    columns = {
        "dealerName": ("dealer_name", payload.dealerName),
        "legalName": ("legal_name", payload.legalName),
        "status": ("status", payload.status),
    }
    assignments: list[str] = []
    parameters: dict[str, object] = {
        "tenant_id": tenant_id,
        "dealer_id": dealer_id,
        "expected_version": expected_version,
        "actor_id": admin_request.user_id,
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
            UPDATE auditcore.dealers
            SET {', '.join(assignments)}
            WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
              AND version_no = :expected_version
            RETURNING dealer_id, dealer_code, dealer_name, legal_name, status, version_no
            """
        ),
        parameters,
    ).mappings().one_or_none()
    if row is None:
        if not _dealer_exists(connection, tenant_id, dealer_id):
            raise _not_found("Dealer")
        raise ConflictError(
            error_code="VAC-CONFLICT-001",
            title="Version conflict",
            detail="Dealer was changed by another request. Refresh and retry.",
        )
    _set_etag(response, row["version_no"])
    return _dealer_response(row)


@router.get(
    "/dealers/{dealer_id}/deletion-impact",
    response_model=DeletionImpactResponse,
)
def dealer_deletion_impact(
    tenant_id: str,
    dealer_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeletionImpactResponse:
    set_tenant_context(connection, tenant_id)
    if not _dealer_exists(connection, tenant_id, dealer_id):
        raise _not_found("Dealer")
    dependencies = _dealer_impact(connection, tenant_id, dealer_id)
    return DeletionImpactResponse(
        canDelete=_can_delete(dependencies),
        dependencies=dependencies,
    )


@router.delete("/dealers/{dealer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dealer(
    tenant_id: str,
    dealer_id: UUID,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> Response:
    set_tenant_context(connection, tenant_id)

    def perform_delete() -> dict[str, object]:
        if not _dealer_exists(connection, tenant_id, dealer_id):
            raise _not_found("Dealer")
        dependencies = _dealer_impact(connection, tenant_id, dealer_id)
        if not _can_delete(dependencies):
            raise BusinessValidationError(
                detail=(
                    "Dealer has dependent Project data and cannot be deleted directly. "
                    "Use the dependency impact to remove safe dependencies or use whole-Project "
                    "hard delete where applicable."
                )
            )
        connection.execute(
            text(
                "DELETE FROM auditcore.dealers "
                "WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id"
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id},
        )
        return {}

    execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key="UC02_DEALER_DELETE",
        idempotency_key=idempotency_key,
        request_payload={"dealerId": str(dealer_id)},
        execute=perform_delete,
        response_status=status.HTTP_204_NO_CONTENT,
        logical_result_id=str(dealer_id),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/dealers/{dealer_id}/outlets",
    response_model=OutletResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_outlet(
    tenant_id: str,
    dealer_id: UUID,
    payload: OutletCreate,
    response: Response,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> OutletResponse:
    set_tenant_context(connection, tenant_id)
    if not _dealer_exists(connection, tenant_id, dealer_id):
        raise _not_found("Dealer")

    outlet_id = uuid4()
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.dealer_outlets (
                tenant_id, dealer_id, outlet_id, outlet_code, outlet_name,
                outlet_classification, address_text, city, state_region, postal_code,
                google_place_id, latitude, longitude, monthly_vehicle_volume,
                created_by_actor_id
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_id, :outlet_code, :outlet_name,
                :classification, :address_text, :city, :state_region, :postal_code,
                :google_place_id, :latitude, :longitude, :monthly_vehicle_volume,
                :actor_id
            )
            RETURNING outlet_id, dealer_id, outlet_code, outlet_name,
                      outlet_classification, address_text, city, state_region, postal_code,
                      google_place_id, latitude, longitude, monthly_vehicle_volume,
                      status, version_no
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "outlet_code": outlet_id.hex,
            "outlet_name": payload.outletName,
            "classification": payload.outletClassification,
            "address_text": payload.addressText,
            "city": payload.city,
            "state_region": payload.stateRegion,
            "postal_code": payload.postalCode,
            "google_place_id": payload.googlePlaceId,
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "monthly_vehicle_volume": payload.monthlyVehicleVolume,
            "actor_id": admin_request.user_id,
        },
    ).mappings().one()
    _set_etag(response, row["version_no"])
    return _outlet_response(row)


@router.get("/dealers/{dealer_id}/outlets", response_model=list[OutletResponse])
def list_outlets(
    tenant_id: str,
    dealer_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[OutletResponse]:
    set_tenant_context(connection, tenant_id)
    rows = connection.execute(
        text(
            """
            SELECT outlet_id, dealer_id, outlet_code, outlet_name,
                   outlet_classification, address_text, city, state_region, postal_code,
                   google_place_id, latitude, longitude, monthly_vehicle_volume,
                   status, version_no
            FROM auditcore.dealer_outlets
            WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
            ORDER BY outlet_code
            """
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id},
    ).mappings()
    return [_outlet_response(row) for row in rows]


@router.get("/dealers/{dealer_id}/outlets/{outlet_id}", response_model=OutletResponse)
def get_outlet(
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
    response: Response,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> OutletResponse:
    set_tenant_context(connection, tenant_id)
    row = connection.execute(
        text(
            """
            SELECT outlet_id, dealer_id, outlet_code, outlet_name,
                   outlet_classification, address_text, city, state_region, postal_code,
                   google_place_id, latitude, longitude, monthly_vehicle_volume,
                   status, version_no
            FROM auditcore.dealer_outlets
            WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
              AND outlet_id = :outlet_id
            """
        ),
        {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
    ).mappings().one_or_none()
    if row is None:
        raise _not_found("Outlet")
    _set_etag(response, row["version_no"])
    return _outlet_response(row)


@router.patch("/dealers/{dealer_id}/outlets/{outlet_id}", response_model=OutletResponse)
def patch_outlet(
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
    payload: OutletPatch,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> OutletResponse:
    set_tenant_context(connection, tenant_id)
    supplied = payload.model_fields_set
    if not supplied:
        raise ValidationError(detail="At least one Outlet field must be supplied.")
    if "outletName" in supplied and payload.outletName is None:
        raise ValidationError(detail="Outlet Name cannot be set to null.")
    if "outletClassification" in supplied and payload.outletClassification is None:
        raise ValidationError(detail="Outlet classification cannot be set to null.")
    if "status" in supplied and payload.status is None:
        raise ValidationError(detail="Outlet status cannot be set to null.")

    expected_version = _parse_if_match(if_match)
    columns = {
        "outletName": ("outlet_name", payload.outletName),
        "outletClassification": ("outlet_classification", payload.outletClassification),
        "addressText": ("address_text", payload.addressText),
        "city": ("city", payload.city),
        "stateRegion": ("state_region", payload.stateRegion),
        "postalCode": ("postal_code", payload.postalCode),
        "googlePlaceId": ("google_place_id", payload.googlePlaceId),
        "latitude": ("latitude", payload.latitude),
        "longitude": ("longitude", payload.longitude),
        "monthlyVehicleVolume": ("monthly_vehicle_volume", payload.monthlyVehicleVolume),
        "status": ("status", payload.status),
    }
    assignments: list[str] = []
    parameters: dict[str, object] = {
        "tenant_id": tenant_id,
        "dealer_id": dealer_id,
        "outlet_id": outlet_id,
        "expected_version": expected_version,
        "actor_id": admin_request.user_id,
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
            UPDATE auditcore.dealer_outlets
            SET {', '.join(assignments)}
            WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
              AND outlet_id = :outlet_id AND version_no = :expected_version
            RETURNING outlet_id, dealer_id, outlet_code, outlet_name,
                      outlet_classification, address_text, city, state_region, postal_code,
                      google_place_id, latitude, longitude, monthly_vehicle_volume,
                      status, version_no
            """
        ),
        parameters,
    ).mappings().one_or_none()
    if row is None:
        if not _outlet_exists(connection, tenant_id, dealer_id, outlet_id):
            raise _not_found("Outlet")
        raise ConflictError(
            error_code="VAC-CONFLICT-001",
            title="Version conflict",
            detail="Outlet was changed by another request. Refresh and retry.",
        )
    _set_etag(response, row["version_no"])
    return _outlet_response(row)


@router.get(
    "/dealers/{dealer_id}/outlets/{outlet_id}/deletion-impact",
    response_model=DeletionImpactResponse,
)
def outlet_deletion_impact(
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeletionImpactResponse:
    set_tenant_context(connection, tenant_id)
    if not _outlet_exists(connection, tenant_id, dealer_id, outlet_id):
        raise _not_found("Outlet")
    dependencies = _outlet_impact(connection, tenant_id, dealer_id, outlet_id)
    return DeletionImpactResponse(
        canDelete=_can_delete(dependencies),
        dependencies=dependencies,
    )


@router.delete(
    "/dealers/{dealer_id}/outlets/{outlet_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_outlet(
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> Response:
    set_tenant_context(connection, tenant_id)

    def perform_delete() -> dict[str, object]:
        if not _outlet_exists(connection, tenant_id, dealer_id, outlet_id):
            raise _not_found("Outlet")
        dependencies = _outlet_impact(connection, tenant_id, dealer_id, outlet_id)
        if not _can_delete(dependencies):
            raise BusinessValidationError(
                detail=(
                    "Dealer Outlet has dependent Project data and cannot be deleted directly. "
                    "Use the dependency impact to remove safe dependencies or use whole-Project "
                    "hard delete where applicable."
                )
            )
        connection.execute(
            text(
                """
                DELETE FROM auditcore.dealer_outlets
                WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id
                  AND outlet_id = :outlet_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
            },
        )
        return {}

    execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key="UC02_OUTLET_DELETE",
        idempotency_key=idempotency_key,
        request_payload={"dealerId": str(dealer_id), "outletId": str(outlet_id)},
        execute=perform_delete,
        response_status=status.HTTP_204_NO_CONTENT,
        logical_result_id=str(outlet_id),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
