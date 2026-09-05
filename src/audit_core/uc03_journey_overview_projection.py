from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import Connection, text

from audit_core import uc03_journey_search as legacy
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/uc03",
    tags=["uc03-journey-overview"],
)


class JourneyOverviewProjectionResponse(legacy.JourneyOverviewResponse):
    receipts: list[dict[str, Any]] = Field(default_factory=list)


_BOOKING_REVIEW_FIELDS = (
    "dealer_name",
    "dealer_branch",
    "booking_reference_number",
    "booking_date",
    "customer_name",
    "customer_phone",
    "customer_email",
    "customer_address",
    "vehicle_model",
    "vehicle_variant",
    "vehicle_color",
    "sku_code",
    "sales_person",
    "registration_by",
    "registration_type",
    "insurance_by",
    "exchange_applicable",
    "exchange_value",
    "ex_showroom_price",
    "insurance_amount",
    "registration_charges",
    "road_tax_amount",
    "road_tax_registration",
    "tcs_amount",
    "rsa_amount",
    "additional_warranty_amount",
    "accessories_cost",
    "other_charges",
    "discount_amount",
    "bonus_amount",
    "total_price",
    "net_amount",
    "booking_amount_paid",
    "balance_amount",
    "mode_of_payment",
    "payment_reference_no",
    "expected_delivery",
    "expected_delivery_date",
    "deal_type",
    "out_of_scope_reasons",
    "dsa_commission_amount",
)


def _normalized_identity(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def _unambiguous(rows: list[dict[str, Any]], field: str) -> Any | None:
    values = [row.get(field) for row in rows]
    values = [value for value in values if value is not None and value != ""]
    if not values:
        return None
    first = values[0]
    normalized = _normalized_identity(first)
    if all(_normalized_identity(value) == normalized for value in values[1:]):
        return first
    return None


def _masked_phone(value: Any) -> str | None:
    if value is None:
        return None
    digits = "".join(character for character in str(value) if character.isdigit())
    if len(digits) < 4:
        return None
    return f"******{digits[-4:]}"


def _stage_review_statuses(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, str]:
    rows = connection.execute(
        text(
            """
            SELECT stage_code, COALESCE(pc_verification_status, 'PENDING') AS review_status
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code IN ('BOOKING','DELIVERY')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return {
        str(row["stage_code"]): str(row["review_status"])
        for row in rows
    }


def _reviewed_booking_rows(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT to_jsonb(v) AS payload
            FROM auditcore.booking_form_review_values v
            WHERE v.tenant_id=:tenant_id AND v.journey_id=:journey_id
            ORDER BY v.reviewed_at_utc DESC, v.booking_form_review_value_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row["payload"]) for row in rows]


def _reviewed_identity_rows(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT document_type_key, pan_name, aadhaar_name
            FROM auditcore.customer_identity_review_values
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            ORDER BY reviewed_at_utc DESC, customer_identity_review_value_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _reviewed_booking_projection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        field: value
        for field in _BOOKING_REVIEW_FIELDS
        if (value := _unambiguous(rows, field)) is not None
    }


def _reviewed_legal_name(rows: list[dict[str, Any]]) -> str | None:
    names: list[dict[str, Any]] = []
    for row in rows:
        value = row.get("pan_name") or row.get("aadhaar_name")
        if value is not None and str(value).strip():
            names.append({"name": value})
    value = _unambiguous(names, "name")
    return str(value) if value is not None else None


def _documents(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    review_statuses: dict[str, str],
) -> list[dict[str, Any]]:
    v2_rows = connection.execute(
        text(
            """
            SELECT
                di_document_id AS "documentId",
                requirement_key AS "requirementKey",
                classified_document_type_key AS "documentTypeKey",
                stage_code AS "processArea",
                capture_status AS "processingStatus",
                original_filename AS "originalFilename",
                created_at_utc AS "linkedAtUtc"
            FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND capture_status <> 'SUPERSEDED'
            ORDER BY created_at_utc, di_document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    documents: list[dict[str, Any]] = []
    seen_document_ids: set[str] = set()
    for row in v2_rows:
        item = dict(row)
        document_id = str(item["documentId"])
        seen_document_ids.add(document_id)
        item.update(
            {
                "evidenceId": None,
                "evidencePurpose": "DOCUMENT_CAPTURE",
                "verificationStatus": None,
                "confirmationStatus": None,
                "reviewStatus": review_statuses.get(str(item.get("processArea"))),
            }
        )
        documents.append(item)

    legacy_rows = connection.execute(
        text(
            """
            SELECT
                e.evidence_id AS "evidenceId",
                e.di_document_id AS "documentId",
                e.document_type_key AS "documentTypeKey",
                e.evidence_purpose AS "evidencePurpose",
                e.process_area AS "processArea",
                e.processing_status_cache AS "processingStatus",
                e.verification_status_cache AS "verificationStatus",
                e.confirmation_status_cache AS "confirmationStatus",
                e.linked_at_utc AS "linkedAtUtc"
            FROM auditcore.evidence e
            WHERE e.tenant_id=:tenant_id AND e.journey_id=:journey_id
              AND e.association_status='ACTIVE'
            ORDER BY e.linked_at_utc, e.evidence_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    for row in legacy_rows:
        item = dict(row)
        document_id = item.get("documentId")
        if document_id is not None and str(document_id) in seen_document_ids:
            continue
        item["reviewStatus"] = review_statuses.get(str(item.get("processArea")))
        documents.append(item)
    return documents


def _payments(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT
                payment_id AS "paymentId",
                payment_at_utc AS "paymentAtUtc",
                amount AS "amount",
                currency_code AS "currencyCode",
                payment_method_code AS "paymentMethodCode",
                payment_reference AS "paymentReference",
                receipt_number AS "receiptNumber",
                receipt_date AS "receiptDate",
                actual_status_code AS "actualStatusCode",
                status_source AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId",
                source_di_document_id AS "sourceDocumentId",
                payment_stage AS "paymentStage",
                receipt_dealer_name AS "dealerName",
                receipt_dealer_gstin AS "dealerGstin",
                receipt_customer_name AS "customerName",
                receipt_customer_phone AS "customerPhone",
                payment_reference_date AS "paymentReferenceDate",
                receipt_bank_name AS "bankName",
                receipt_bank_location AS "bankLocation",
                receipt_booking_reference AS "bookingReference",
                receipt_remarks AS "remarks",
                receipt_amount_in_words AS "amountInWords"
            FROM auditcore.payments
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            ORDER BY receipt_date NULLS LAST, payment_at_utc NULLS LAST,
                     created_at_utc, payment_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("customerPhone"):
            item["customerPhone"] = _masked_phone(item["customerPhone"])
        result.append(item)
    return result


def _receipts(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    review_statuses: dict[str, str],
) -> list[dict[str, Any]]:
    reviewed_rows = connection.execute(
        text(
            """
            SELECT
                r.source_di_document_id AS "documentId",
                r.source_evidence_id AS "evidenceId",
                r.dealer_name AS "dealerName",
                r.dealer_gstin AS "dealerGstin",
                r.customer_name AS "customerName",
                r.customer_phone AS "customerPhone",
                r.receipt_number AS "receiptNumber",
                r.receipt_date AS "receiptDate",
                r.amount_paid AS "amount",
                r.payment_mode AS "paymentMethodCode",
                r.payment_reference_no AS "paymentReference",
                r.payment_reference_date AS "paymentReferenceDate",
                r.bank_name AS "bankName",
                r.bank_location AS "bankLocation",
                r.booking_reference_number AS "bookingReference",
                r.remarks AS "remarks",
                r.amount_in_words AS "amountInWords",
                d.original_filename AS "originalFilename",
                COALESCE(d.stage_code, 'BOOKING') AS "stageCode",
                COALESCE(d.capture_status, 'CLASSIFIED') AS "captureStatus"
            FROM auditcore.dealer_receipt_review_values r
            LEFT JOIN auditcore.document_capture_v2_documents d
              ON d.tenant_id=r.tenant_id
             AND d.journey_id=r.journey_id
             AND d.di_document_id=r.source_di_document_id
            WHERE r.tenant_id=:tenant_id AND r.journey_id=:journey_id
            ORDER BY r.reviewed_at_utc, r.dealer_receipt_review_value_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    result: list[dict[str, Any]] = []
    reviewed_ids: set[str] = set()
    for row in reviewed_rows:
        item = dict(row)
        reviewed_ids.add(str(item["documentId"]))
        item["reviewStatus"] = "VERIFIED"
        if item.get("customerPhone"):
            item["customerPhone"] = _masked_phone(item["customerPhone"])
        result.append(item)

    pending_rows = connection.execute(
        text(
            """
            SELECT
                di_document_id AS "documentId",
                original_filename AS "originalFilename",
                stage_code AS "stageCode",
                capture_status AS "captureStatus",
                classified_document_type_key AS "documentTypeKey"
            FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND capture_status <> 'SUPERSEDED'
              AND classified_document_type_key='dealer_receipt'
            ORDER BY created_at_utc, di_document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    for row in pending_rows:
        item = dict(row)
        if str(item["documentId"]) in reviewed_ids:
            continue
        item["reviewStatus"] = review_statuses.get(str(item.get("stageCode")), "PENDING")
        result.append(item)
    return result


@router.get(
    "/journeys/{journey_id}/overview",
    response_model=JourneyOverviewProjectionResponse,
)
def get_journey_overview_projection(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> JourneyOverviewProjectionResponse:
    """Project the current V2 Journey state without reading raw DI business facts."""

    base = legacy.get_journey_overview(
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
    )
    data = base.model_dump()
    review_statuses = _stage_review_statuses(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )

    journey = dict(data["journey"])
    journey["bookingPcVerificationStatus"] = review_statuses.get("BOOKING")
    journey["deliveryPcVerificationStatus"] = review_statuses.get("DELIVERY")
    data["journey"] = journey

    reviewed_booking_rows = _reviewed_booking_rows(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    reviewed_booking = _reviewed_booking_projection(reviewed_booking_rows)

    customer = dict(data["customer"])
    if not customer.get("legalName"):
        customer["legalName"] = _reviewed_legal_name(
            _reviewed_identity_rows(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
            )
        )
    if not customer.get("emailReference") and reviewed_booking.get("customer_email"):
        customer["emailReference"] = reviewed_booking["customer_email"]
    if not customer.get("mobileNumber"):
        customer["mobileNumber"] = _masked_phone(
            reviewed_booking.get("customer_phone") or customer.get("mobileLast4")
        )
    data["customer"] = customer

    booking = dict(data.get("booking") or {})
    fallback_fields = {
        "bookingReference": "booking_reference_number",
        "bookingDate": "booking_date",
        "dealType": "deal_type",
        "modelName": "vehicle_model",
        "variantName": "vehicle_variant",
        "colourName": "vehicle_color",
    }
    for destination, source in fallback_fields.items():
        if not booking.get(destination) and reviewed_booking.get(source) is not None:
            booking[destination] = reviewed_booking[source]
    booking["reviewedValues"] = reviewed_booking
    data["booking"] = booking or None

    data["payments"] = _payments(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    data["receipts"] = _receipts(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        review_statuses=review_statuses,
    )
    data["evidence"] = _documents(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        review_statuses=review_statuses,
    )
    return JourneyOverviewProjectionResponse.model_validate(data)
