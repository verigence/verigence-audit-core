from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["vehicle-delivery"])
SourceKind = Literal["EVIDENCE", "OPERATIONAL_INPUT", "SOURCE_SYSTEM"]


class VehiclePut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vin: str | None = Field(default=None, max_length=120)
    chassisNumber: str | None = Field(default=None, max_length=120)
    dmsReference: str | None = Field(default=None, max_length=160)
    invoiceReference: str | None = Field(default=None, max_length=160)
    allocatedAtUtc: datetime | None = None
    sourceKind: SourceKind | None = None
    sourceEvidenceId: UUID | None = None


class VehicleResponse(VehiclePut):
    vehicleRecordId: UUID
    journeyId: UUID


class RegistrationPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registrationState: str | None = Field(default=None, max_length=160)
    registrationTerritory: str | None = Field(default=None, max_length=160)
    registrationDistrict: str | None = Field(default=None, max_length=160)
    registrationTypeCode: str | None = Field(default=None, max_length=100)
    registrationCategoryCode: str | None = Field(default=None, max_length=100)
    registrationNumber: str | None = Field(default=None, max_length=120)
    actualStatusCode: str | None = Field(default=None, max_length=100)
    sourceKind: SourceKind | None = None
    sourceEvidenceId: UUID | None = None


class RegistrationResponse(RegistrationPut):
    registrationRecordId: UUID
    journeyId: UUID


class DeliveryPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plannedDeliveryAt: datetime | None = None
    deliveryIntimatedAt: datetime | None = None
    actualDeliveryStatusCode: str | None = Field(default=None, max_length=100)
    actualDeliveredAt: datetime | None = None
    statusSource: SourceKind | None = None
    sourceEvidenceId: UUID | None = None

    @model_validator(mode="after")
    def require_status_source(self):
        if self.actualDeliveryStatusCode is not None and self.statusSource is None:
            raise ValueError("statusSource is required when actualDeliveryStatusCode is supplied")
        return self


class DeliveryStatusHistoryResponse(BaseModel):
    deliveryStatusHistoryId: UUID
    actualDeliveryStatusCode: str
    statusLabel: str | None
    actualDeliveredAt: datetime | None
    statusSource: str
    sourceEvidenceId: UUID | None
    recordedAtUtc: datetime


class DeliveryResponse(DeliveryPut):
    deliveryId: UUID
    journeyId: UUID
    statusLabel: str | None
    history: list[DeliveryStatusHistoryResponse]


def _journey_scope(connection: Connection, tenant_id: str, journey_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Journey not found",
            detail="Journey not found for the requested tenant.",
        )
    return row


def _scope(
    connection: Connection,
    principal: Principal,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> None:
    journey = _journey_scope(connection, tenant_id, journey_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )


def _one_or_not_found(connection: Connection, sql: str, params: dict, title: str):
    row = connection.execute(text(sql), params).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-012",
            title=f"{title} not found",
            detail=f"{title} not found for the requested Journey.",
        )
    return row


def _vehicle(connection: Connection, tenant_id: str, journey_id: UUID):
    row = _one_or_not_found(
        connection,
        """
        SELECT vehicle_record_id, journey_id, vin, chassis_number,
               dms_reference, invoice_reference, allocated_at_utc,
               source_kind, source_evidence_id
        FROM auditcore.vehicle_records
        WHERE tenant_id = :tenant_id AND journey_id = :journey_id
        """,
        {"tenant_id": tenant_id, "journey_id": journey_id},
        "Vehicle record",
    )
    return VehicleResponse(
        vehicleRecordId=row["vehicle_record_id"],
        journeyId=row["journey_id"],
        vin=row["vin"],
        chassisNumber=row["chassis_number"],
        dmsReference=row["dms_reference"],
        invoiceReference=row["invoice_reference"],
        allocatedAtUtc=row["allocated_at_utc"],
        sourceKind=row["source_kind"],
        sourceEvidenceId=row["source_evidence_id"],
    )


@router.get("/journeys/{journey_id}/vehicle", response_model=VehicleResponse)
def get_vehicle(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> VehicleResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.read")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    return _vehicle(connection, tenant_id, journey_id)


@router.put("/journeys/{journey_id}/vehicle", response_model=VehicleResponse)
def put_vehicle(
    tenant_id: str,
    journey_id: UUID,
    payload: VehiclePut,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> VehicleResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.update")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    connection.execute(
        text(
            """
            INSERT INTO auditcore.vehicle_records (
                tenant_id, journey_id, vin, chassis_number, dms_reference,
                invoice_reference, allocated_at_utc, source_kind, source_evidence_id
            ) VALUES (
                :tenant_id, :journey_id, :vin, :chassis_number, :dms_reference,
                :invoice_reference, :allocated_at_utc, :source_kind, :source_evidence_id
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                vin = EXCLUDED.vin,
                chassis_number = EXCLUDED.chassis_number,
                dms_reference = EXCLUDED.dms_reference,
                invoice_reference = EXCLUDED.invoice_reference,
                allocated_at_utc = EXCLUDED.allocated_at_utc,
                source_kind = EXCLUDED.source_kind,
                source_evidence_id = EXCLUDED.source_evidence_id,
                updated_at_utc = now(),
                version_no = auditcore.vehicle_records.version_no + 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "vin": payload.vin,
            "chassis_number": payload.chassisNumber,
            "dms_reference": payload.dmsReference,
            "invoice_reference": payload.invoiceReference,
            "allocated_at_utc": payload.allocatedAtUtc,
            "source_kind": payload.sourceKind,
            "source_evidence_id": payload.sourceEvidenceId,
        },
    )
    return _vehicle(connection, tenant_id, journey_id)


def _registration(connection: Connection, tenant_id: str, journey_id: UUID):
    row = _one_or_not_found(
        connection,
        """
        SELECT registration_record_id, journey_id, registration_state,
               registration_territory, registration_district,
               registration_type_code, registration_category_code,
               registration_number, actual_status_code, source_kind,
               source_evidence_id
        FROM auditcore.registration_records
        WHERE tenant_id = :tenant_id AND journey_id = :journey_id
        """,
        {"tenant_id": tenant_id, "journey_id": journey_id},
        "Registration record",
    )
    return RegistrationResponse(
        registrationRecordId=row["registration_record_id"],
        journeyId=row["journey_id"],
        registrationState=row["registration_state"],
        registrationTerritory=row["registration_territory"],
        registrationDistrict=row["registration_district"],
        registrationTypeCode=row["registration_type_code"],
        registrationCategoryCode=row["registration_category_code"],
        registrationNumber=row["registration_number"],
        actualStatusCode=row["actual_status_code"],
        sourceKind=row["source_kind"],
        sourceEvidenceId=row["source_evidence_id"],
    )


@router.get("/journeys/{journey_id}/registration", response_model=RegistrationResponse)
def get_registration(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> RegistrationResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.read")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    return _registration(connection, tenant_id, journey_id)


@router.put("/journeys/{journey_id}/registration", response_model=RegistrationResponse)
def put_registration(
    tenant_id: str,
    journey_id: UUID,
    payload: RegistrationPut,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> RegistrationResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.update")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    connection.execute(
        text(
            """
            INSERT INTO auditcore.registration_records (
                tenant_id, journey_id, registration_state, registration_territory,
                registration_district, registration_type_code,
                registration_category_code, registration_number,
                actual_status_code, source_kind, source_evidence_id
            ) VALUES (
                :tenant_id, :journey_id, :registration_state, :registration_territory,
                :registration_district, :registration_type_code,
                :registration_category_code, :registration_number,
                :actual_status_code, :source_kind, :source_evidence_id
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                registration_state = EXCLUDED.registration_state,
                registration_territory = EXCLUDED.registration_territory,
                registration_district = EXCLUDED.registration_district,
                registration_type_code = EXCLUDED.registration_type_code,
                registration_category_code = EXCLUDED.registration_category_code,
                registration_number = EXCLUDED.registration_number,
                actual_status_code = EXCLUDED.actual_status_code,
                source_kind = EXCLUDED.source_kind,
                source_evidence_id = EXCLUDED.source_evidence_id,
                updated_at_utc = now(),
                version_no = auditcore.registration_records.version_no + 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "registration_state": payload.registrationState,
            "registration_territory": payload.registrationTerritory,
            "registration_district": payload.registrationDistrict,
            "registration_type_code": payload.registrationTypeCode,
            "registration_category_code": payload.registrationCategoryCode,
            "registration_number": payload.registrationNumber,
            "actual_status_code": payload.actualStatusCode,
            "source_kind": payload.sourceKind,
            "source_evidence_id": payload.sourceEvidenceId,
        },
    )
    return _registration(connection, tenant_id, journey_id)


def _delivery_history(connection: Connection, tenant_id: str, delivery_id: UUID):
    rows = connection.execute(
        text(
            """
            SELECT delivery_status_history_id, actual_delivery_status_code,
                   status_label_snapshot, actual_delivered_at, status_source,
                   source_evidence_id, recorded_at_utc
            FROM auditcore.delivery_status_history
            WHERE tenant_id = :tenant_id AND delivery_id = :delivery_id
            ORDER BY recorded_at_utc, delivery_status_history_id
            """
        ),
        {"tenant_id": tenant_id, "delivery_id": delivery_id},
    ).mappings().all()
    return [
        DeliveryStatusHistoryResponse(
            deliveryStatusHistoryId=row["delivery_status_history_id"],
            actualDeliveryStatusCode=row["actual_delivery_status_code"],
            statusLabel=row["status_label_snapshot"],
            actualDeliveredAt=row["actual_delivered_at"],
            statusSource=row["status_source"],
            sourceEvidenceId=row["source_evidence_id"],
            recordedAtUtc=row["recorded_at_utc"],
        )
        for row in rows
    ]


def _delivery(connection: Connection, tenant_id: str, journey_id: UUID):
    row = _one_or_not_found(
        connection,
        """
        SELECT delivery_id, journey_id, planned_delivery_at,
               delivery_intimated_at, actual_delivery_status_code,
               status_label_snapshot, actual_delivered_at, status_source,
               source_evidence_id
        FROM auditcore.deliveries
        WHERE tenant_id = :tenant_id AND journey_id = :journey_id
        """,
        {"tenant_id": tenant_id, "journey_id": journey_id},
        "Delivery record",
    )
    return DeliveryResponse(
        deliveryId=row["delivery_id"],
        journeyId=row["journey_id"],
        plannedDeliveryAt=row["planned_delivery_at"],
        deliveryIntimatedAt=row["delivery_intimated_at"],
        actualDeliveryStatusCode=row["actual_delivery_status_code"],
        statusLabel=row["status_label_snapshot"],
        actualDeliveredAt=row["actual_delivered_at"],
        statusSource=row["status_source"],
        sourceEvidenceId=row["source_evidence_id"],
        history=_delivery_history(connection, tenant_id, row["delivery_id"]),
    )


def _delivery_status_label(connection: Connection, tenant_id: str, status_code: str):
    label = connection.execute(
        text(
            """
            SELECT status_label
            FROM auditcore.business_status_codes
            WHERE tenant_id = :tenant_id
              AND domain_key = 'DELIVERY'
              AND status_code = :status_code
              AND is_active = true
            """
        ),
        {"tenant_id": tenant_id, "status_code": status_code},
    ).scalar_one_or_none()
    if label is None:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Invalid delivery status",
            detail="Delivery status must be an active configured DELIVERY business code.",
        )
    return label


@router.get("/journeys/{journey_id}/delivery", response_model=DeliveryResponse)
def get_delivery(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeliveryResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.delivery.read")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    return _delivery(connection, tenant_id, journey_id)


@router.put("/journeys/{journey_id}/delivery", response_model=DeliveryResponse)
def put_delivery(
    tenant_id: str,
    journey_id: UUID,
    payload: DeliveryPut,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DeliveryResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.delivery.write")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)

    label = None
    if payload.actualDeliveryStatusCode is not None:
        label = _delivery_status_label(
            connection, tenant_id, payload.actualDeliveryStatusCode
        )

    previous = connection.execute(
        text(
            """
            SELECT delivery_id, actual_delivery_status_code
            FROM auditcore.deliveries
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    delivery_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.deliveries (
                tenant_id, journey_id, planned_delivery_at, delivery_intimated_at,
                actual_delivery_status_code, status_label_snapshot,
                actual_delivered_at, status_source, source_evidence_id,
                recorded_by_actor_id
            ) VALUES (
                :tenant_id, :journey_id, :planned_delivery_at, :delivery_intimated_at,
                :status_code, :status_label, :actual_delivered_at,
                :status_source, :source_evidence_id, :actor_id
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                planned_delivery_at = EXCLUDED.planned_delivery_at,
                delivery_intimated_at = EXCLUDED.delivery_intimated_at,
                actual_delivery_status_code = EXCLUDED.actual_delivery_status_code,
                status_label_snapshot = EXCLUDED.status_label_snapshot,
                actual_delivered_at = EXCLUDED.actual_delivered_at,
                status_source = EXCLUDED.status_source,
                source_evidence_id = EXCLUDED.source_evidence_id,
                recorded_by_actor_id = EXCLUDED.recorded_by_actor_id,
                updated_at_utc = now(),
                version_no = auditcore.deliveries.version_no + 1
            RETURNING delivery_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "planned_delivery_at": payload.plannedDeliveryAt,
            "delivery_intimated_at": payload.deliveryIntimatedAt,
            "status_code": payload.actualDeliveryStatusCode,
            "status_label": label,
            "actual_delivered_at": payload.actualDeliveredAt,
            "status_source": payload.statusSource,
            "source_evidence_id": payload.sourceEvidenceId,
            "actor_id": principal.subject,
        },
    ).scalar_one()

    if (
        payload.actualDeliveryStatusCode is not None
        and (previous is None or previous["actual_delivery_status_code"] != payload.actualDeliveryStatusCode)
    ):
        connection.execute(
            text(
                """
                INSERT INTO auditcore.delivery_status_history (
                    tenant_id, delivery_id, journey_id,
                    actual_delivery_status_code, status_label_snapshot,
                    actual_delivered_at, status_source, source_evidence_id,
                    recorded_by_actor_id
                ) VALUES (
                    :tenant_id, :delivery_id, :journey_id,
                    :status_code, :status_label, :actual_delivered_at,
                    :status_source, :source_evidence_id, :actor_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "delivery_id": delivery_id,
                "journey_id": journey_id,
                "status_code": payload.actualDeliveryStatusCode,
                "status_label": label,
                "actual_delivered_at": payload.actualDeliveredAt,
                "status_source": payload.statusSource,
                "source_evidence_id": payload.sourceEvidenceId,
                "actor_id": principal.subject,
            },
        )

    return _delivery(connection, tenant_id, journey_id)
