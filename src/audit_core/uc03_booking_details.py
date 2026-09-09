from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, ConflictError
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _parse_if_match,
    _require_expected_version,
    _stage_state,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/booking/details",
    tags=["uc03-booking-details"],
)

_ACTIVE_BOOKING_STATUSES = {"BOOKING_STARTED", "BOOKING_IN_PROGRESS"}
_DI_AUDIENCE = "di"
_PRICE_LIST_RULE = "BK_PRICE_LIST_NOT_CONFIGURED"
_CORPORATE_ID_RULE = "BK_CORPORATE_ID_NOT_AVAILABLE"
_GST_DOCUMENT_RULE = "BK_GST_CERTIFICATE_NOT_AVAILABLE"

_MASTER_DOMAINS = {
    "customerType": "CUSTOMER_TYPE",
    "dealType": "DEAL_TYPE",
    "dealSource": "DEAL_SOURCE",
    "leadSource": "LEAD_SOURCE",
    "registrationState": "REGISTRATION_STATE",
    "territoryCategorization": "TERRITORY_CATEGORIZATION",
    "districtName": "DISTRICT",
    "registrationType": "REGISTRATION_TYPE",
    "registrationCategory": "REGISTRATION_CATEGORY",
}


class ReferenceOption(BaseModel):
    code: str
    label: str


class PriceListOption(BaseModel):
    priceListId: UUID
    code: str
    name: str
    effectiveVersionId: UUID


class OptionalEvidenceView(BaseModel):
    requirementKey: str
    documentTypeKey: str
    evidenceId: UUID | None = None
    processingStatus: str | None = None


class BookingDetailsOptionsResponse(BaseModel):
    effectiveOn: str
    priceLists: list[PriceListOption]
    customerTypes: list[ReferenceOption]
    dealTypes: list[ReferenceOption]
    dealSources: list[ReferenceOption]
    leadSources: list[ReferenceOption]
    registrationStates: list[ReferenceOption]
    territoryCategories: list[ReferenceOption]
    districts: list[ReferenceOption]
    registrationTypes: list[ReferenceOption]
    registrationCategories: list[ReferenceOption]


class BookingDetailsCommand(BaseModel):
    """Only PC-entered Booking facts for Screen 2."""

    model_config = ConfigDict(extra="forbid")

    priceListId: UUID | None = None
    customerType: str = Field(min_length=1, max_length=80)
    dealType: str = Field(min_length=1, max_length=100)
    dealSource: str = Field(min_length=1, max_length=100)
    leadSource: str = Field(min_length=1, max_length=100)
    registrationState: str = Field(min_length=1, max_length=160)
    territoryCategorization: str = Field(min_length=1, max_length=160)
    districtName: str = Field(min_length=1, max_length=160)
    registrationType: str = Field(min_length=1, max_length=100)
    registrationCategory: str = Field(min_length=1, max_length=100)
    outrightPurchase: bool
    tradeIn: bool
    gstBenefit: bool
    corporateIdAvailable: bool | None = None

    @model_validator(mode="after")
    def validate_conditionals(self):
        if self.customerType.strip().upper() == "CORPORATE" and self.corporateIdAvailable is None:
            raise ValueError("Corporate ID availability is required for a Corporate Booking")
        return self


class BookingDetailsView(BaseModel):
    aggregateVersion: int
    priceListId: UUID | None = None
    customerType: str | None = None
    dealType: str | None = None
    dealSource: str | None = None
    leadSource: str | None = None
    registrationState: str | None = None
    territoryCategorization: str | None = None
    districtName: str | None = None
    registrationType: str | None = None
    registrationCategory: str | None = None
    outrightPurchase: bool | None = None
    tradeIn: bool | None = None
    gstBenefit: bool | None = None
    corporateIdAvailable: bool | None = None
    optionalEvidence: list[OptionalEvidenceView] = Field(default_factory=list)


class BookingDetailsSaveResponse(BaseModel):
    journeyId: UUID
    aggregateVersion: int
    optionalEvidence: list[OptionalEvidenceView]


# ReviewEvidenceItem, BookingReviewStartResponse, DocumentApprovalResponse
# removed (Phase 0 dead-code cleanup) along with start_booking_review and
# approve_review_document below -- see that removal note.


def _require_active(state) -> None:
    if state is None or state["business_status"] not in _ACTIVE_BOOKING_STATUSES:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking state conflict",
            detail="The Booking must be active before Booking Details can change.",
        )


def _effective_date(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str:
    value = connection.execute(
        text(
            """
            SELECT COALESCE(b.booking_date, CURRENT_DATE)
            FROM auditcore.journeys j
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
            WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    return value.isoformat()


def _master_options(
    connection: Connection,
    *,
    tenant_id: str,
    domain: str,
    effective_on: str,
) -> list[ReferenceOption]:
    rows = connection.execute(
        text(
            """
            SELECT status_code, status_label
            FROM auditcore.business_status_codes
            WHERE tenant_id=:tenant_id AND domain_key=:domain
              AND is_active=true
              AND (effective_from IS NULL OR effective_from <= CAST(:effective_on AS date))
              AND (effective_to IS NULL OR effective_to >= CAST(:effective_on AS date))
            ORDER BY status_label, status_code
            """
        ),
        {"tenant_id": tenant_id, "domain": domain, "effective_on": effective_on},
    ).mappings().all()
    return [ReferenceOption(code=row["status_code"], label=row["status_label"]) for row in rows]


def _validate_master(
    connection: Connection,
    *,
    tenant_id: str,
    domain: str,
    value: str,
    effective_on: str,
    label: str,
) -> str:
    code = value.strip().upper()
    found = connection.execute(
        text(
            """
            SELECT 1
            FROM auditcore.business_status_codes
            WHERE tenant_id=:tenant_id AND domain_key=:domain AND status_code=:code
              AND is_active=true
              AND (effective_from IS NULL OR effective_from <= CAST(:effective_on AS date))
              AND (effective_to IS NULL OR effective_to >= CAST(:effective_on AS date))
            """
        ),
        {
            "tenant_id": tenant_id,
            "domain": domain,
            "code": code,
            "effective_on": effective_on,
        },
    ).scalar_one_or_none()
    if found is None:
        raise AuditCoreError(
            error_code="VAC-MASTER-002",
            status_code=422,
            title="Booking master value unavailable",
            detail=f"{label} must be selected from the effective Project master.",
        )
    return code


def _price_list_options(
    connection: Connection,
    *,
    tenant_id: str,
    effective_on: str,
) -> list[PriceListOption]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (pl.price_list_id)
                   pl.price_list_id, pl.price_list_code, pl.price_list_name,
                   plv.price_list_version_id
            FROM auditcore.price_lists pl
            JOIN auditcore.price_list_versions plv
              ON plv.tenant_id=pl.tenant_id AND plv.price_list_id=pl.price_list_id
            WHERE pl.tenant_id=:tenant_id
              AND plv.lifecycle_status='PUBLISHED'
              AND plv.effective_from <= CAST(:effective_on AS date)
              AND (plv.effective_to IS NULL OR plv.effective_to >= CAST(:effective_on AS date))
            ORDER BY pl.price_list_id, plv.version_no DESC
            """
        ),
        {"tenant_id": tenant_id, "effective_on": effective_on},
    ).mappings().all()
    result = [
        PriceListOption(
            priceListId=row["price_list_id"],
            code=row["price_list_code"],
            name=row["price_list_name"],
            effectiveVersionId=row["price_list_version_id"],
        )
        for row in rows
    ]
    return sorted(result, key=lambda item: (item.name.lower(), item.code.lower()))


def _validate_price_list(
    connection: Connection,
    *,
    tenant_id: str,
    price_list_id: UUID,
    effective_on: str,
) -> None:
    found = connection.execute(
        text(
            """
            SELECT 1
            FROM auditcore.price_lists pl
            JOIN auditcore.price_list_versions plv
              ON plv.tenant_id=pl.tenant_id AND plv.price_list_id=pl.price_list_id
            WHERE pl.tenant_id=:tenant_id AND pl.price_list_id=:price_list_id
              AND plv.lifecycle_status='PUBLISHED'
              AND plv.effective_from <= CAST(:effective_on AS date)
              AND (plv.effective_to IS NULL OR plv.effective_to >= CAST(:effective_on AS date))
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "price_list_id": price_list_id,
            "effective_on": effective_on,
        },
    ).scalar_one_or_none()
    if found is None:
        raise AuditCoreError(
            error_code="VAC-MASTER-002",
            status_code=422,
            title="No effective Price List",
            detail="Price List must be selected from the effective Project master.",
        )


def _optional_evidence(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[OptionalEvidenceView]:
    rows = connection.execute(
        text(
            """
            SELECT jdr.requirement_key, jdr.document_type_key,
                   e.evidence_id, e.processing_status_cache
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN LATERAL (
                SELECT evidence_id, processing_status_cache
                FROM auditcore.evidence e
                WHERE e.tenant_id=jdr.tenant_id
                  AND e.journey_document_requirement_id=jdr.journey_document_requirement_id
                  AND e.association_status='ACTIVE'
                ORDER BY e.linked_at_utc DESC, e.evidence_id DESC
                LIMIT 1
            ) e ON true
            WHERE jdr.tenant_id=:tenant_id AND jdr.journey_id=:journey_id
              AND jdr.requirement_key IN ('corporate_id','gst_certificate','trade_in_vehicle_rc')
            ORDER BY CASE jdr.requirement_key
                WHEN 'corporate_id' THEN 10
                WHEN 'gst_certificate' THEN 20
                ELSE 30 END
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        OptionalEvidenceView(
            requirementKey=row["requirement_key"],
            documentTypeKey=row["document_type_key"],
            evidenceId=row["evidence_id"],
            processingStatus=row["processing_status_cache"],
        )
        for row in rows
    ]


def _set_optional_requirement(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirement_key: str,
    document_type_key: str,
    applies: bool,
    reason: str,
) -> None:
    row = connection.execute(
        text(
            """
            SELECT journey_document_requirement_id
            FROM auditcore.journey_document_requirements
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND requirement_key=:requirement_key
            FOR UPDATE
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "requirement_key": requirement_key,
        },
    ).mappings().one_or_none()
    snapshot = json.dumps(
        {
            "applicabilityState": "APPLICABLE" if applies else "NOT_APPLICABLE",
            "applicabilityReason": reason,
        }
    )
    if row is None:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, requirement_key, document_type_key,
                    process_area, requirement_level, requirement_status,
                    condition_snapshot
                ) VALUES (
                    :tenant_id, :journey_id, :requirement_key, :document_type_key,
                    'BOOKING', 'OPTIONAL', :status, CAST(:snapshot AS jsonb)
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "requirement_key": requirement_key,
                "document_type_key": document_type_key,
                "status": "PENDING" if applies else "NOT_APPLICABLE",
                "snapshot": snapshot,
            },
        )
        return

    requirement_id = row["journey_document_requirement_id"]
    has_evidence = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.evidence
            WHERE tenant_id=:tenant_id
              AND journey_document_requirement_id=:requirement_id
              AND association_status='ACTIVE'
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "requirement_id": requirement_id},
    ).scalar_one_or_none()
    status = "SATISFIED" if applies and has_evidence is not None else "PENDING"
    if not applies:
        status = "NOT_APPLICABLE"
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_document_requirements
            SET document_type_key=:document_type_key,
                process_area='BOOKING', requirement_level='OPTIONAL',
                requirement_status=:status,
                condition_snapshot=CAST(:snapshot AS jsonb), updated_at_utc=now()
            WHERE tenant_id=:tenant_id
              AND journey_document_requirement_id=:requirement_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "requirement_id": requirement_id,
            "document_type_key": document_type_key,
            "status": status,
            "snapshot": snapshot,
        },
    )


def _details_view(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> BookingDetailsView:
    state = connection.execute(
        text(
            """
            SELECT version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    booking = connection.execute(
        text(
            """
            SELECT c.customer_type_code,
                   b.price_list_id, b.deal_type_code, b.deal_source_code,
                   b.lead_source_code, b.outright_purchase,
                   b.corporate_id_available, b.gst_benefit
            FROM auditcore.journeys j
            JOIN auditcore.customers c
              ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
            WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()
    registration = connection.execute(
        text(
            """
            SELECT registration_state, registration_territory,
                   registration_district, registration_type_code,
                   registration_category_code
            FROM auditcore.registration_records
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    trade_status = connection.execute(
        text(
            """
            SELECT actual_status_code
            FROM auditcore.trade_in_cases
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    trade_in: bool | None = None
    if trade_status is not None:
        trade_in = str(trade_status).upper() == "EXCHANGE_TAKEN"
    return BookingDetailsView(
        aggregateVersion=int(state["version_no"]) if state is not None else 0,
        priceListId=booking["price_list_id"],
        customerType=booking["customer_type_code"],
        dealType=booking["deal_type_code"],
        dealSource=booking["deal_source_code"],
        leadSource=booking["lead_source_code"],
        registrationState=registration["registration_state"] if registration else None,
        territoryCategorization=registration["registration_territory"] if registration else None,
        districtName=registration["registration_district"] if registration else None,
        registrationType=registration["registration_type_code"] if registration else None,
        registrationCategory=registration["registration_category_code"] if registration else None,
        outrightPurchase=booking["outright_purchase"],
        tradeIn=trade_in,
        gstBenefit=booking["gst_benefit"],
        corporateIdAvailable=booking["corporate_id_available"],
        optionalEvidence=_optional_evidence(connection, tenant_id=tenant_id, journey_id=journey_id),
    )


# _review_documents and _record_machine_observation removed (Phase 0
# dead-code cleanup) -- both were only used by start_booking_review /
# approve_review_document below, which are removed too.


@router.get("", response_model=BookingDetailsView)
def get_booking_details(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingDetailsView:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _details_view(connection, tenant_id=tenant_id, journey_id=journey_id)


@router.get("/options", response_model=BookingDetailsOptionsResponse)
def get_booking_details_options(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingDetailsOptionsResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    effective_on = _effective_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    return BookingDetailsOptionsResponse(
        effectiveOn=effective_on,
        priceLists=_price_list_options(connection, tenant_id=tenant_id, effective_on=effective_on),
        customerTypes=_master_options(connection, tenant_id=tenant_id, domain=_MASTER_DOMAINS["customerType"], effective_on=effective_on),
        dealTypes=_master_options(connection, tenant_id=tenant_id, domain=_MASTER_DOMAINS["dealType"], effective_on=effective_on),
        dealSources=_master_options(connection, tenant_id=tenant_id, domain=_MASTER_DOMAINS["dealSource"], effective_on=effective_on),
        leadSources=_master_options(connection, tenant_id=tenant_id, domain=_MASTER_DOMAINS["leadSource"], effective_on=effective_on),
        registrationStates=_master_options(connection, tenant_id=tenant_id, domain=_MASTER_DOMAINS["registrationState"], effective_on=effective_on),
        territoryCategories=_master_options(connection, tenant_id=tenant_id, domain=_MASTER_DOMAINS["territoryCategorization"], effective_on=effective_on),
        districts=_master_options(connection, tenant_id=tenant_id, domain=_MASTER_DOMAINS["districtName"], effective_on=effective_on),
        registrationTypes=_master_options(connection, tenant_id=tenant_id, domain=_MASTER_DOMAINS["registrationType"], effective_on=effective_on),
        registrationCategories=_master_options(connection, tenant_id=tenant_id, domain=_MASTER_DOMAINS["registrationCategory"], effective_on=effective_on),
    )


@router.put("", response_model=BookingDetailsSaveResponse)
def save_booking_details(
    request: Request,
    tenant_id: str,
    journey_id: UUID,
    command: BookingDetailsCommand,
    if_match: Annotated[str, Header(alias="If-Match")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingDetailsSaveResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
    state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    _require_active(state)
    _require_expected_version(state, _parse_if_match(if_match))
    effective_on = _effective_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if command.priceListId is not None:
        _validate_price_list(
            connection,
            tenant_id=tenant_id,
            price_list_id=command.priceListId,
            effective_on=effective_on,
        )
    values: dict[str, str] = {}
    for field_name, domain in _MASTER_DOMAINS.items():
        raw = getattr(command, field_name)
        values[field_name] = _validate_master(
            connection,
            tenant_id=tenant_id,
            domain=domain,
            value=raw,
            effective_on=effective_on,
            label=field_name,
        )

    customer_id = connection.execute(
        text(
            "SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:tenant_id AND journey_id=:journey_id"
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            UPDATE auditcore.customers
            SET customer_type_code=:customer_type, updated_at_utc=now(), version_no=version_no+1
            WHERE tenant_id=:tenant_id AND customer_id=:customer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "customer_type": values["customerType"],
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.bookings (
                tenant_id, journey_id, price_list_id, deal_type_code,
                deal_source_code, lead_source_code, outright_purchase,
                corporate_id_available, gst_benefit
            ) VALUES (
                :tenant_id, :journey_id, :price_list_id, :deal_type,
                :deal_source, :lead_source, :outright_purchase,
                :corporate_id_available, :gst_benefit
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                price_list_id=EXCLUDED.price_list_id,
                deal_type_code=EXCLUDED.deal_type_code,
                deal_source_code=EXCLUDED.deal_source_code,
                lead_source_code=EXCLUDED.lead_source_code,
                outright_purchase=EXCLUDED.outright_purchase,
                corporate_id_available=EXCLUDED.corporate_id_available,
                gst_benefit=EXCLUDED.gst_benefit,
                updated_at_utc=now(), version_no=auditcore.bookings.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "price_list_id": command.priceListId,
            "deal_type": values["dealType"],
            "deal_source": values["dealSource"],
            "lead_source": values["leadSource"],
            "outright_purchase": command.outrightPurchase,
            "corporate_id_available": command.corporateIdAvailable
            if values["customerType"] == "CORPORATE"
            else None,
            "gst_benefit": command.gstBenefit,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.registration_records (
                tenant_id, journey_id, registration_state,
                registration_territory, registration_district,
                registration_type_code, registration_category_code, source_kind
            ) VALUES (
                :tenant_id, :journey_id, :registration_state,
                :territory, :district, :registration_type,
                :registration_category, 'OPERATIONAL_INPUT'
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                registration_state=EXCLUDED.registration_state,
                registration_territory=EXCLUDED.registration_territory,
                registration_district=EXCLUDED.registration_district,
                registration_type_code=EXCLUDED.registration_type_code,
                registration_category_code=EXCLUDED.registration_category_code,
                source_kind='OPERATIONAL_INPUT', source_evidence_id=NULL,
                updated_at_utc=now(), version_no=auditcore.registration_records.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "registration_state": values["registrationState"],
            "territory": values["territoryCategorization"],
            "district": values["districtName"],
            "registration_type": values["registrationType"],
            "registration_category": values["registrationCategory"],
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.trade_in_cases (
                tenant_id, journey_id, actual_status_code, source_kind
            ) VALUES (
                :tenant_id, :journey_id, :status_code, 'OPERATIONAL_INPUT'
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                actual_status_code=EXCLUDED.actual_status_code,
                source_kind='OPERATIONAL_INPUT', source_evidence_id=NULL,
                updated_at_utc=now(), version_no=auditcore.trade_in_cases.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "status_code": "EXCHANGE_TAKEN" if command.tradeIn else "NO_EXCHANGE",
        },
    )

    _set_optional_requirement(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirement_key="corporate_id",
        document_type_key="corporate_id",
        applies=values["customerType"] == "CORPORATE",
        reason=f"customerType={values['customerType']}",
    )
    _set_optional_requirement(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirement_key="gst_certificate",
        document_type_key="gst_certificate",
        applies=command.gstBenefit,
        reason=f"gstBenefit={'Yes' if command.gstBenefit else 'No'}",
    )
    _set_optional_requirement(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirement_key="trade_in_vehicle_rc",
        document_type_key="vehicle_rc",
        applies=command.tradeIn,
        reason=f"tradeIn={'Yes' if command.tradeIn else 'No'}",
    )

    next_version = int(state["version_no"]) + 1
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET business_status='BOOKING_IN_PROGRESS',
                audit_state=CASE WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS' ELSE audit_state END,
                latest_activity_at_utc=now(), updated_at_utc=now(), version_no=:version
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
    )
    _append_workflow_event(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        event_type="BOOKING_DETAILS_CAPTURED",
        source_kind="HUMAN",
        actor_id=human_principal.subject,
        actor_role_snapshot=context["operating_role"],
        idempotency_key=idempotency_key,
        correlation_id=get_correlation_id(request),
        safe_payload={
            "customerType": values["customerType"],
            "tradeIn": command.tradeIn,
            "gstBenefit": command.gstBenefit,
            "priceListConfigured": command.priceListId is not None,
        },
        aggregate_version=next_version,
    )
    return BookingDetailsSaveResponse(
        journeyId=journey_id,
        aggregateVersion=next_version,
        optionalEvidence=_optional_evidence(connection, tenant_id=tenant_id, journey_id=journey_id),
    )


# start_booking_review (POST .../review) and approve_review_document
# (POST .../review/{evidence_id}/approve) removed (Phase 0 dead-code
# cleanup): confirmed zero web callers (uc03BookingJourney.ts's
# startBookingDetailsReview/approveBookingReviewDocument wrappers exist
# but nothing in verigence-web calls either), zero test coverage, and no
# api/openapi-v1.yaml entry.
