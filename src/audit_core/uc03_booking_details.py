from __future__ import annotations

import json
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
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
_CORPORATE_ID_RULE = "BK_CORPORATE_ID_NOT_AVAILABLE"
_GST_DOCUMENT_RULE = "BK_GST_CERTIFICATE_NOT_AVAILABLE"


class BookingDetailsCommand(BaseModel):
    """PC-entered Booking facts for the dedicated Booking Details screen."""

    model_config = ConfigDict(extra="forbid")

    priceListId: UUID
    corporateCustomer: bool
    dealType: str = Field(min_length=1, max_length=100)
    dealSource: str = Field(min_length=1, max_length=100)
    leadSource: str = Field(min_length=1, max_length=100)
    registrationState: str = Field(min_length=1, max_length=160)
    territoryCategorization: str = Field(min_length=1, max_length=160)
    districtName: str = Field(min_length=1, max_length=160)
    registrationType: str = Field(min_length=1, max_length=100)
    registrationCategory: str = Field(min_length=1, max_length=100)
    outrightPurchase: bool

    accessoriesTaken: bool | None = None
    fasttagTaken: bool | None = None
    greenTax: bool | None = None
    otherCharges: Decimal | None = Field(default=None, ge=0)
    hpCharges: Decimal | None = Field(default=None, ge=0)

    exchangeTaken: bool
    exchangeDiscountTaken: bool | None = None
    tradeInRcAvailable: bool | None = None
    tradeInSameOwner: bool | None = None
    exchangeDiscountType: str | None = Field(default=None, max_length=120)

    corporateDiscountTaken: bool | None = None
    corporateDiscountType: str | None = Field(default=None, max_length=120)
    corporateIdAvailable: bool | None = None
    gstBenefit: bool

    @model_validator(mode="after")
    def validate_conditionals(self):
        if self.corporateCustomer and self.corporateIdAvailable is None:
            raise ValueError("Corporate ID availability is required for a Corporate Booking")
        if self.corporateDiscountTaken and not (self.corporateDiscountType or "").strip():
            raise ValueError("Corporate Discount Type is required when Corporate Discount is taken")
        if self.exchangeDiscountTaken and not (self.exchangeDiscountType or "").strip():
            raise ValueError("Exchange Discount Type is required when Exchange Discount is taken")
        return self


class PriceListOption(BaseModel):
    priceListId: UUID
    code: str
    name: str
    effectiveVersionId: UUID


class BookingDetailsOptionsResponse(BaseModel):
    effectiveOn: str
    priceLists: list[PriceListOption]


class OptionalEvidenceState(BaseModel):
    corporateIdEvidenceId: UUID | None
    gstCertificateEvidenceId: UUID | None


class BookingDetailsView(BaseModel):
    aggregateVersion: int
    priceListId: UUID | None
    corporateCustomer: bool | None
    dealType: str | None
    dealSource: str | None
    leadSource: str | None
    registrationState: str | None
    territoryCategorization: str | None
    districtName: str | None
    registrationType: str | None
    registrationCategory: str | None
    outrightPurchase: bool | None
    accessoriesTaken: bool | None
    fasttagTaken: bool | None
    greenTax: bool | None
    otherCharges: Decimal | None
    hpCharges: Decimal | None
    exchangeTaken: bool | None
    exchangeDiscountTaken: bool | None
    tradeInRcAvailable: bool | None
    tradeInSameOwner: bool | None
    exchangeDiscountType: str | None
    corporateDiscountTaken: bool | None
    corporateDiscountType: str | None
    corporateIdAvailable: bool | None
    gstBenefit: bool | None
    optionalEvidence: OptionalEvidenceState


class BookingDetailsSaveResponse(BaseModel):
    journeyId: UUID
    aggregateVersion: int


class ReviewEvidenceItem(BaseModel):
    evidenceId: UUID
    documentTypeKey: str | None
    evidencePurpose: str
    processingStatus: str | None
    verificationStatus: str | None


class BookingReviewStartResponse(BaseModel):
    journeyId: UUID
    aggregateVersion: int
    raisedObservationIds: list[UUID]
    documents: list[ReviewEvidenceItem]


def _require_active(state) -> None:
    if state is None or state["business_status"] not in _ACTIVE_BOOKING_STATUSES:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking state conflict",
            detail="The Booking must be active before Booking Details can change.",
        )


def _optional_evidence(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> OptionalEvidenceState:
    rows = connection.execute(
        text(
            """
            SELECT evidence_id, document_type_key, evidence_purpose
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND association_status='ACTIVE'
              AND (
                    document_type_key IN ('corporate_id', 'gst_certificate')
                    OR evidence_purpose IN (
                        'UC03_BOOKING:CORPORATE_ID',
                        'UC03_BOOKING:GST_CERTIFICATE'
                    )
              )
            ORDER BY linked_at_utc DESC, evidence_id DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    corporate_id: UUID | None = None
    gst: UUID | None = None
    for row in rows:
        doc_type = (row["document_type_key"] or "").lower()
        purpose = row["evidence_purpose"] or ""
        if corporate_id is None and (
            doc_type == "corporate_id" or purpose == "UC03_BOOKING:CORPORATE_ID"
        ):
            corporate_id = row["evidence_id"]
        if gst is None and (
            doc_type == "gst_certificate" or purpose == "UC03_BOOKING:GST_CERTIFICATE"
        ):
            gst = row["evidence_id"]
    return OptionalEvidenceState(
        corporateIdEvidenceId=corporate_id,
        gstCertificateEvidenceId=gst,
    )


def _trade_details(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


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
    aggregate_version = int(state["version_no"]) if state is not None else 0

    booking = connection.execute(
        text(
            """
            SELECT b.price_list_id, b.deal_type_code, b.deal_source_code,
                   b.lead_source_code, b.outright_purchase, b.accessories_taken,
                   b.fasttag_taken, b.green_tax, b.other_charges, b.hp_charges,
                   b.exchange_discount_taken, b.corporate_customer,
                   b.corporate_discount_taken, b.corporate_discount_type,
                   b.corporate_id_available, b.gst_benefit,
                   c.customer_type_code
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

    trade = connection.execute(
        text(
            """
            SELECT actual_status_code, details
            FROM auditcore.trade_in_cases
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    trade_details = _trade_details(trade["details"] if trade is not None else None)
    exchange_taken = None
    if trade is not None and trade["actual_status_code"]:
        exchange_taken = trade["actual_status_code"] == "EXCHANGE_TAKEN"

    corporate_value = booking["corporate_customer"]
    if corporate_value is None and booking["customer_type_code"] is not None:
        corporate_value = str(booking["customer_type_code"]).upper() == "CORPORATE"

    return BookingDetailsView(
        aggregateVersion=aggregate_version,
        priceListId=booking["price_list_id"],
        corporateCustomer=corporate_value,
        dealType=booking["deal_type_code"],
        dealSource=booking["deal_source_code"],
        leadSource=booking["lead_source_code"],
        registrationState=registration["registration_state"] if registration else None,
        territoryCategorization=registration["registration_territory"] if registration else None,
        districtName=registration["registration_district"] if registration else None,
        registrationType=registration["registration_type_code"] if registration else None,
        registrationCategory=registration["registration_category_code"] if registration else None,
        outrightPurchase=booking["outright_purchase"],
        accessoriesTaken=booking["accessories_taken"],
        fasttagTaken=booking["fasttag_taken"],
        greenTax=booking["green_tax"],
        otherCharges=booking["other_charges"],
        hpCharges=booking["hp_charges"],
        exchangeTaken=exchange_taken,
        exchangeDiscountTaken=booking["exchange_discount_taken"],
        tradeInRcAvailable=trade_details.get("rcAvailable"),
        tradeInSameOwner=trade_details.get("sameOwner"),
        exchangeDiscountType=trade_details.get("exchangeDiscountType"),
        corporateDiscountTaken=booking["corporate_discount_taken"],
        corporateDiscountType=booking["corporate_discount_type"],
        corporateIdAvailable=booking["corporate_id_available"],
        gstBenefit=booking["gst_benefit"],
        optionalEvidence=_optional_evidence(
            connection, tenant_id=tenant_id, journey_id=journey_id
        ),
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


def _price_list_options(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> BookingDetailsOptionsResponse:
    effective_on = _effective_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (pl.price_list_id)
                   pl.price_list_id, pl.price_list_code, pl.price_list_name,
                   plv.price_list_version_id
            FROM auditcore.price_lists pl
            JOIN auditcore.price_list_versions plv
              ON plv.tenant_id=pl.tenant_id
             AND plv.price_list_id=pl.price_list_id
            WHERE pl.tenant_id=:tenant_id
              AND plv.lifecycle_status='PUBLISHED'
              AND plv.effective_from <= CAST(:effective_on AS date)
              AND (plv.effective_to IS NULL OR plv.effective_to >= CAST(:effective_on AS date))
            ORDER BY pl.price_list_id, plv.version_no DESC
            """
        ),
        {"tenant_id": tenant_id, "effective_on": effective_on},
    ).mappings().all()
    options = sorted(
        [
            PriceListOption(
                priceListId=row["price_list_id"],
                code=row["price_list_code"],
                name=row["price_list_name"],
                effectiveVersionId=row["price_list_version_id"],
            )
            for row in rows
        ],
        key=lambda item: (item.name.lower(), item.code.lower()),
    )
    return BookingDetailsOptionsResponse(effectiveOn=effective_on, priceLists=options)


def _validate_price_list(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    price_list_id: UUID,
) -> None:
    effective_on = _effective_date(connection, tenant_id=tenant_id, journey_id=journey_id)
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
            title="No effective master version",
            detail="The selected Price List is not effective for this Booking date.",
        )


def _record_machine_observation(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    rule_key: str,
    title: str,
    description: str,
    correlation_id: str,
) -> UUID:
    existing = connection.execute(
        text(
            """
            SELECT audit_finding_id
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND rule_key=:rule_key
              AND finding_status <> 'VOIDED'
            ORDER BY created_at_utc DESC, audit_finding_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "rule_key": rule_key},
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    finding_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_findings (
                tenant_id, journey_id, finding_type_code, severity,
                finding_status, title, description, created_by_actor_id,
                correlation_id, stage_code, origin_kind, origin_actor_id,
                origin_role_snapshot, rule_key, blocking_completion
            ) VALUES (
                :tenant_id, :journey_id, 'DOCUMENT_EXCEPTION', 'INFO',
                'OPEN', :title, :description, NULL,
                :correlation_id, 'BOOKING', 'MACHINE', NULL,
                'SYSTEM', :rule_key, false
            )
            RETURNING audit_finding_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "title": title,
            "description": description,
            "correlation_id": correlation_id,
            "rule_key": rule_key,
        },
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot,
                safe_payload, correlation_id
            ) VALUES (
                :tenant_id, :finding_id, :journey_id, 'BOOKING',
                'RAISED', NULL, 'SYSTEM', CAST(:safe_payload AS jsonb),
                :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "journey_id": journey_id,
            "safe_payload": json.dumps({"originKind": "MACHINE", "ruleKey": rule_key}),
            "correlation_id": correlation_id,
        },
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET audit_status='FLAGS_RAISED', updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    )
    return finding_id


def _review_documents(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[ReviewEvidenceItem]:
    rows = connection.execute(
        text(
            """
            SELECT evidence_id, document_type_key, evidence_purpose,
                   processing_status_cache, verification_status_cache,
                   linked_at_utc
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND association_status='ACTIVE'
              AND (
                    upper(COALESCE(process_area, 'BOOKING'))='BOOKING'
                    OR evidence_purpose LIKE 'UC03_BOOKING:%'
              )
            ORDER BY
              CASE lower(COALESCE(document_type_key, ''))
                WHEN 'booking_docket' THEN 10
                WHEN 'booking_form' THEN 10
                WHEN 'pan_card' THEN 20
                WHEN 'pan' THEN 20
                WHEN 'aadhaar' THEN 30
                WHEN 'dealer_receipt' THEN 40
                WHEN 'minimum_booking_payment_proof' THEN 40
                WHEN 'corporate_id' THEN 50
                WHEN 'gst_certificate' THEN 60
                ELSE 90
              END,
              linked_at_utc,
              evidence_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        ReviewEvidenceItem(
            evidenceId=row["evidence_id"],
            documentTypeKey=row["document_type_key"],
            evidencePurpose=row["evidence_purpose"],
            processingStatus=row["processing_status_cache"],
            verificationStatus=row["verification_status_cache"],
        )
        for row in rows
    ]


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
    return _price_list_options(connection, tenant_id=tenant_id, journey_id=journey_id)


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
    _validate_price_list(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        price_list_id=command.priceListId,
    )

    customer_id = connection.execute(
        text("SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:tenant_id AND journey_id=:journey_id"),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            UPDATE auditcore.customers
            SET customer_type_code=:customer_type, updated_at_utc=now(),
                version_no=version_no+1
            WHERE tenant_id=:tenant_id AND customer_id=:customer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "customer_type": "CORPORATE" if command.corporateCustomer else "INDIVIDUAL",
        },
    )

    connection.execute(
        text(
            """
            INSERT INTO auditcore.bookings (
                tenant_id, journey_id, price_list_id,
                deal_type_code, deal_source_code, lead_source_code,
                outright_purchase, accessories_taken, fasttag_taken,
                green_tax, other_charges, hp_charges,
                exchange_discount_taken, corporate_customer,
                corporate_discount_taken, corporate_discount_type,
                corporate_id_available, gst_benefit
            ) VALUES (
                :tenant_id, :journey_id, :price_list_id,
                :deal_type, :deal_source, :lead_source,
                :outright_purchase, :accessories_taken, :fasttag_taken,
                :green_tax, :other_charges, :hp_charges,
                :exchange_discount_taken, :corporate_customer,
                :corporate_discount_taken, :corporate_discount_type,
                :corporate_id_available, :gst_benefit
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                price_list_id=EXCLUDED.price_list_id,
                deal_type_code=EXCLUDED.deal_type_code,
                deal_source_code=EXCLUDED.deal_source_code,
                lead_source_code=EXCLUDED.lead_source_code,
                outright_purchase=EXCLUDED.outright_purchase,
                accessories_taken=EXCLUDED.accessories_taken,
                fasttag_taken=EXCLUDED.fasttag_taken,
                green_tax=EXCLUDED.green_tax,
                other_charges=EXCLUDED.other_charges,
                hp_charges=EXCLUDED.hp_charges,
                exchange_discount_taken=EXCLUDED.exchange_discount_taken,
                corporate_customer=EXCLUDED.corporate_customer,
                corporate_discount_taken=EXCLUDED.corporate_discount_taken,
                corporate_discount_type=EXCLUDED.corporate_discount_type,
                corporate_id_available=EXCLUDED.corporate_id_available,
                gst_benefit=EXCLUDED.gst_benefit,
                updated_at_utc=now(),
                version_no=auditcore.bookings.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "price_list_id": command.priceListId,
            "deal_type": command.dealType.strip(),
            "deal_source": command.dealSource.strip(),
            "lead_source": command.leadSource.strip(),
            "outright_purchase": command.outrightPurchase,
            "accessories_taken": command.accessoriesTaken,
            "fasttag_taken": command.fasttagTaken,
            "green_tax": command.greenTax,
            "other_charges": command.otherCharges,
            "hp_charges": command.hpCharges,
            "exchange_discount_taken": command.exchangeDiscountTaken,
            "corporate_customer": command.corporateCustomer,
            "corporate_discount_taken": command.corporateDiscountTaken,
            "corporate_discount_type": (command.corporateDiscountType or "").strip() or None,
            "corporate_id_available": command.corporateIdAvailable if command.corporateCustomer else None,
            "gst_benefit": command.gstBenefit,
        },
    )

    connection.execute(
        text(
            """
            INSERT INTO auditcore.registration_records (
                tenant_id, journey_id, registration_state,
                registration_territory, registration_district,
                registration_type_code, registration_category_code,
                source_kind
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
                updated_at_utc=now(),
                version_no=auditcore.registration_records.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "registration_state": command.registrationState.strip(),
            "territory": command.territoryCategorization.strip(),
            "district": command.districtName.strip(),
            "registration_type": command.registrationType.strip(),
            "registration_category": command.registrationCategory.strip(),
        },
    )

    trade_details = {
        "rcAvailable": command.tradeInRcAvailable if command.exchangeTaken else None,
        "sameOwner": command.tradeInSameOwner if command.exchangeTaken else None,
        "exchangeDiscountType": (
            (command.exchangeDiscountType or "").strip() or None
            if command.exchangeTaken
            else None
        ),
    }
    connection.execute(
        text(
            """
            INSERT INTO auditcore.trade_in_cases (
                tenant_id, journey_id, actual_status_code, source_kind, details
            ) VALUES (
                :tenant_id, :journey_id, :status_code,
                'OPERATIONAL_INPUT', CAST(:details AS jsonb)
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                actual_status_code=EXCLUDED.actual_status_code,
                source_kind='OPERATIONAL_INPUT', source_evidence_id=NULL,
                details=COALESCE(auditcore.trade_in_cases.details, '{}'::jsonb)
                        || EXCLUDED.details,
                updated_at_utc=now(),
                version_no=auditcore.trade_in_cases.version_no+1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "status_code": "EXCHANGE_TAKEN" if command.exchangeTaken else "NO_EXCHANGE",
            "details": json.dumps(trade_details),
        },
    )

    next_version = int(state["version_no"]) + 1
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET business_status='BOOKING_IN_PROGRESS',
                audit_state=CASE WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS' ELSE audit_state END,
                latest_activity_at_utc=now(), updated_at_utc=now(),
                version_no=:version
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
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
            "corporateCustomer": command.corporateCustomer,
            "exchangeTaken": command.exchangeTaken,
            "gstBenefit": command.gstBenefit,
        },
        aggregate_version=next_version,
    )
    return BookingDetailsSaveResponse(journeyId=journey_id, aggregateVersion=next_version)


@router.post("/review", response_model=BookingReviewStartResponse)
def start_booking_review(
    request: Request,
    tenant_id: str,
    journey_id: UUID,
    if_match: Annotated[str, Header(alias="If-Match")],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingReviewStartResponse:
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

    details = _details_view(connection, tenant_id=tenant_id, journey_id=journey_id)
    required_values = {
        "Price List": details.priceListId,
        "Corporate / Individual": details.corporateCustomer,
        "Type of Deal": details.dealType,
        "Deal Source": details.dealSource,
        "Lead Generated Through": details.leadSource,
        "Registration State": details.registrationState,
        "Territory Categorization": details.territoryCategorization,
        "District Name": details.districtName,
        "Registration Type": details.registrationType,
        "Registration Category": details.registrationCategory,
        "Outright Purchase": details.outrightPurchase,
        "Exchange Taken": details.exchangeTaken,
        "GST Benefit": details.gstBenefit,
    }
    missing = [label for label, value in required_values.items() if value is None or value == ""]
    if details.corporateCustomer and details.corporateIdAvailable is None:
        missing.append("Corporate ID availability")
    if missing:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Booking Details incomplete",
            detail="Complete the required Booking Details before review: " + ", ".join(missing),
        )

    observations: list[UUID] = []
    evidence = details.optionalEvidence
    correlation_id = get_correlation_id(request)
    if details.corporateCustomer and (
        details.corporateIdAvailable is False or evidence.corporateIdEvidenceId is None
    ):
        observations.append(
            _record_machine_observation(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                rule_key=_CORPORATE_ID_RULE,
                title="Corporate ID not available",
                description=(
                    "The Booking is marked Corporate but a Corporate ID document was not supplied "
                    "before document review. The evidence remains optional and Booking may continue."
                ),
                correlation_id=correlation_id,
            )
        )
    if details.gstBenefit and evidence.gstCertificateEvidenceId is None:
        observations.append(
            _record_machine_observation(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                rule_key=_GST_DOCUMENT_RULE,
                title="GST Certificate not available",
                description=(
                    "GST benefit is marked Yes but a GST Certificate was not supplied before "
                    "document review. The evidence remains optional and Booking may continue."
                ),
                correlation_id=correlation_id,
            )
        )

    next_version = int(state["version_no"]) + 1
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET business_status='BOOKING_IN_PROGRESS',
                audit_state=CASE WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS' ELSE audit_state END,
                latest_activity_at_utc=now(), updated_at_utc=now(),
                version_no=:version
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
    )
    _append_workflow_event(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        event_type="BOOKING_DOCUMENT_REVIEW_STARTED",
        source_kind="HUMAN",
        actor_id=human_principal.subject,
        actor_role_snapshot=context["operating_role"],
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        safe_payload={"observationCount": len(observations)},
        aggregate_version=next_version,
    )
    return BookingReviewStartResponse(
        journeyId=journey_id,
        aggregateVersion=next_version,
        raisedObservationIds=observations,
        documents=_review_documents(connection, tenant_id=tenant_id, journey_id=journey_id),
    )
