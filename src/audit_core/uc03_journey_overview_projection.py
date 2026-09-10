from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import Connection, text

from audit_core import uc03_journey_reviewed_details as reviewed_details
from audit_core import uc03_journey_search as legacy
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_masters_alignment import (
    canonical_discount_key,
    commercial_key_for_price_component,
    registration_basis,
)
from audit_core.uc03_model_resolution import sync_model_resolution
from audit_core.uc03_payment_reconciliation import reconcile_payments

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/uc03",
    tags=["uc03-journey-overview"],
)


class JourneyOverviewProjectionResponse(legacy.JourneyOverviewResponse):
    receipts: list[dict[str, Any]] = Field(default_factory=list)
    reviewedFields: list[dict[str, Any]] = Field(default_factory=list)
    resolvedReviewedValues: dict[str, dict[str, Any]] = Field(default_factory=dict)
    skuPricing: dict[str, Any] | None = Field(default=None)
    bankStatementLines: list[dict[str, Any]] = Field(default_factory=list)
    invoices: list[dict[str, Any]] = Field(default_factory=list)
    scrappageCertificates: list[dict[str, Any]] = Field(default_factory=list)


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
    """Resolve legal name from KYC, using PAN then Aadhaar as the tie-break."""

    for field in ("pan_name", "aadhaar_name"):
        for row in rows:
            value = row.get(field)
            if value is not None and str(value).strip():
                return str(value)
    return None


def _resolved_value(
    resolved: dict[str, dict[str, Any]],
    semantic_key: str,
) -> Any | None:
    item = resolved.get(semantic_key)
    if not item:
        return None
    value = item.get("value")
    return value if value is not None and value != "" else None


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
                COALESCE(d.capture_status, 'CLASSIFIED') AS "captureStatus",
                m.match_status AS "bankMatchStatus",
                m.match_method AS "bankMatchMethod",
                m.bank_statement_line_id AS "bankMatchLineId",
                bl.reference_no AS "bankMatchLineReference",
                bl.transaction_date AS "bankMatchLineDate"
            FROM auditcore.dealer_receipt_review_values r
            LEFT JOIN auditcore.document_capture_v2_documents d
              ON d.tenant_id=r.tenant_id
             AND d.journey_id=r.journey_id
             AND d.di_document_id=r.source_di_document_id
            LEFT JOIN auditcore.payments p
              ON p.tenant_id=r.tenant_id
             AND p.journey_id=r.journey_id
             AND p.source_di_document_id=r.source_di_document_id
            LEFT JOIN auditcore.payment_bank_matches m
              ON m.tenant_id=p.tenant_id AND m.payment_id=p.payment_id
            LEFT JOIN auditcore.bank_statement_lines bl
              ON bl.tenant_id=m.tenant_id
             AND bl.bank_statement_line_id=m.bank_statement_line_id
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
        status = item.pop("bankMatchStatus", None)
        method = item.pop("bankMatchMethod", None)
        line_id = item.pop("bankMatchLineId", None)
        line_ref = item.pop("bankMatchLineReference", None)
        line_date = item.pop("bankMatchLineDate", None)
        item["bankMatch"] = (
            {
                "status": status,
                "method": method,
                "lineId": str(line_id) if line_id is not None else None,
                "lineReference": line_ref,
                "lineDate": line_date,
            }
            if status is not None
            else None
        )
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
        item["bankMatch"] = None
        result.append(item)
    return result


def _bank_statement_lines(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT
                bl.bank_statement_line_id AS "bankStatementLineId",
                bl.source_di_document_id  AS "documentId",
                bl.bank_name              AS "bankName",
                bl.account_number         AS "accountNumber",
                bl.transaction_date       AS "transactionDate",
                bl.value_date             AS "valueDate",
                bl.transaction_description AS "description",
                bl.reference_no           AS "referenceNo",
                bl.counterparty_name      AS "counterpartyName",
                bl.debit_amount           AS "debitAmount",
                bl.credit_amount          AS "creditAmount",
                bl.running_balance        AS "runningBalance",
                bl.manually_flagged       AS "manuallyFlagged",
                m.payment_id              AS "matchedPaymentId",
                m.match_status            AS "matchStatus"
            FROM auditcore.bank_statement_lines bl
            LEFT JOIN auditcore.payment_bank_matches m
              ON m.tenant_id=bl.tenant_id
             AND m.bank_statement_line_id=bl.bank_statement_line_id
             AND m.match_status='MATCHED'
            WHERE bl.tenant_id=:tenant_id AND bl.journey_id=:journey_id
            ORDER BY bl.transaction_date NULLS LAST, bl.bank_statement_line_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("accountNumber"):
            item["accountNumber"] = _mask_account(str(item["accountNumber"]))
        matched_payment = item.pop("matchedPaymentId", None)
        item["matchedPaymentId"] = str(matched_payment) if matched_payment is not None else None
        item["bankStatementLineId"] = str(item["bankStatementLineId"])
        if item.get("documentId") is not None:
            item["documentId"] = str(item["documentId"])
        out.append(item)
    return out


def _invoices(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Every reviewed invoice-family document on this journey (tax/retail/
    accessory/EW/RSA invoices, wholesale invoices, and credit notes -- one
    row per document, invoice_review_values already stores them separately
    rather than collapsing multiple invoices into one). Feeds the Invoice
    tab so a PC/TL can see every invoice uploaded without opening each
    document individually.
    """
    rows = connection.execute(
        text(
            """
            SELECT
                invoice_review_value_id AS "invoiceReviewValueId",
                source_di_document_id   AS "documentId",
                document_type_key       AS "documentTypeKey",
                invoice_purpose         AS "invoicePurpose",
                invoice_nature          AS "invoiceNature",
                invoice_number          AS "invoiceNumber",
                invoice_date            AS "invoiceDate",
                seller_name             AS "sellerName",
                seller_gstin            AS "sellerGstin",
                buyer_name              AS "buyerName",
                buyer_gstin             AS "buyerGstin",
                financed_by             AS "financedBy",
                taxable_amount          AS "taxableAmount",
                cgst_amount             AS "cgstAmount",
                sgst_amount             AS "sgstAmount",
                igst_amount             AS "igstAmount",
                tcs_amount              AS "tcsAmount",
                invoice_discount_amount AS "invoiceDiscountAmount",
                grand_total_amount      AS "grandTotalAmount",
                amount_in_words         AS "amountInWords",
                line_items              AS "lineItems",
                reviewed_at_utc         AS "reviewedAtUtc"
            FROM auditcore.invoice_review_values
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            ORDER BY invoice_date NULLS LAST, invoice_review_value_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["invoiceReviewValueId"] = str(item["invoiceReviewValueId"])
        if item.get("documentId") is not None:
            item["documentId"] = str(item["documentId"])
        out.append(item)
    return out


def _scrappage_certificates(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Every reviewed Vehicle Scrappage Certificate of Deposit on this
    journey -- one row per document, since a journey can hold both the
    original Certificate of Deposit and a Transfer Certificate of Deposit
    recording its resale. Feeds the old-vehicle/scrappage evidence a
    PC/TL needs to verify the Booking Form's own scrappage_discount_amount.
    """
    rows = connection.execute(
        text(
            """
            SELECT
                scrappage_certificate_review_value_id AS "scrappageCertificateReviewValueId",
                source_di_document_id                 AS "documentId",
                certificate_variant                   AS "certificateVariant",
                certificate_number                    AS "certificateNumber",
                old_vehicle_registration_number       AS "oldVehicleRegistrationNumber",
                old_vehicle_make                      AS "oldVehicleMake",
                old_vehicle_model                      AS "oldVehicleModel",
                old_vehicle_category                  AS "oldVehicleCategory",
                old_vehicle_type                       AS "oldVehicleType",
                old_vehicle_fuel_type                  AS "oldVehicleFuelType",
                old_vehicle_cubic_capacity             AS "oldVehicleCubicCapacity",
                old_vehicle_seating_capacity           AS "oldVehicleSeatingCapacity",
                old_vehicle_year_of_manufacturing      AS "oldVehicleYearOfManufacturing",
                old_vehicle_unladen_weight_kg          AS "oldVehicleUnladenWeightKg",
                old_vehicle_number_of_cylinders        AS "oldVehicleNumberOfCylinders",
                old_vehicle_gross_vehicle_weight_kg    AS "oldVehicleGrossVehicleWeightKg",
                old_vehicle_wheelbase_mm               AS "oldVehicleWheelbaseMm",
                original_owner_name                    AS "originalOwnerName",
                current_holder_name                    AS "currentHolderName",
                current_holder_mobile                  AS "currentHolderMobile",
                current_holder_pan                     AS "currentHolderPan",
                trade_date                             AS "tradeDate",
                trade_number                           AS "tradeNumber",
                certificate_issue_date                 AS "certificateIssueDate",
                certificate_valid_until_date           AS "certificateValidUntilDate",
                scrapping_facility_name                AS "scrappingFacilityName",
                rvsf_registration_number               AS "rvsfRegistrationNumber",
                state_of_scrapping                     AS "stateOfScrapping",
                reviewed_at_utc                        AS "reviewedAtUtc"
            FROM auditcore.scrappage_certificate_review_values
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            ORDER BY reviewed_at_utc, scrappage_certificate_review_value_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["scrappageCertificateReviewValueId"] = str(item["scrappageCertificateReviewValueId"])
        if item.get("documentId") is not None:
            item["documentId"] = str(item["documentId"])
        out.append(item)
    return out


def _mask_account(value: str) -> str:
    digits = value.strip()
    if len(digits) <= 4:
        return digits
    return f"{'X' * (len(digits) - 4)}{digits[-4:]}"


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _discount_origin(row: dict[str, Any]) -> str:
    details = row.get("details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            details = {}
    if isinstance(details, dict):
        return str(details.get("origin") or "")
    return ""


def _actual_source_rank(row: dict[str, Any]) -> int:
    """Precedence for the *given* discount amount: an invoice beats the booking
    form, which beats the reconciliation's own COALESCE fill."""
    origin = _discount_origin(row)
    if origin == "INVOICE_MATERIALIZATION":
        return 0
    if str(row.get("sourceKind") or "").upper() == "EVIDENCE":
        return 1
    if origin == "DEAL_RECONCILIATION" or str(row.get("sourceKind") or "").upper() == "CALCULATED":
        return 2
    return 3


def _collapse_discounts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per discount key. ``uc03_deal_reconciliation`` writes a canonical
    CALCULATED row carrying the entitled amount; ``uc03_invoice_materialization``
    and the booking-form materialiser write the given amount (invoice wins). Keep
    the entitled/standard side from the reconciliation row and take the given side
    from the strongest source."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        canonical = canonical_discount_key(str(row.get("discountKey") or ""))
        if canonical not in grouped:
            grouped[canonical] = []
            order.append(canonical)
        grouped[canonical].append(row)

    collapsed: list[dict[str, Any]] = []
    for canonical in order:
        bucket = grouped[canonical]
        standard_carrier = next(
            (r for r in bucket if r.get("standardEligibleAmount") is not None), bucket[0]
        )
        actual_carrier = min(
            (r for r in bucket if r.get("actualDiscountAmount") is not None),
            key=_actual_source_rank,
            default=None,
        )
        merged = dict(standard_carrier)
        merged["discountKey"] = canonical
        if actual_carrier is not None:
            merged["actualDiscountAmount"] = actual_carrier.get("actualDiscountAmount")
            if _actual_source_rank(actual_carrier) < _actual_source_rank(standard_carrier):
                merged["sourceKind"] = actual_carrier.get("sourceKind")
                merged["sourceEvidenceId"] = actual_carrier.get("sourceEvidenceId")
        collapsed.append(merged)
    return collapsed


def _sku_pricing_panel(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    reviewed_booking: dict[str, Any],
) -> dict[str, Any] | None:
    """Build a master-vs-booking price comparison panel for the Journey 360 view.

    Returns None when the journey has no resolved SKU yet (no journey_products row).
    When a SKU is resolved, returns a dict with two sides:
      - master*   : values read directly from the effective price-list master
      - booking*  : values extracted from the Booking Form evidence (DI-reviewed)
    and deviation amounts/percentages for numeric fields that are present on both sides.
    """

    sku_row = connection.execute(
        text(
            """
            SELECT
                jp.product_sku_id,
                jp.model_name_snapshot    AS model_name,
                jp.variant_name_snapshot  AS variant_name,
                jp.colour_name_snapshot   AS colour_name,
                jp.selection_status,
                jp.selection_method,
                s.sku_code,
                b.price_list_id
            FROM auditcore.journey_products jp
            JOIN auditcore.product_skus s
              ON s.product_sku_id = jp.product_sku_id
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id = jp.tenant_id
             AND b.journey_id = jp.journey_id
            WHERE jp.tenant_id=:tenant_id AND jp.journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    if sku_row is None:
        return None

    product_sku_id = sku_row["product_sku_id"]

    # Fetch all active price-list items for this SKU so we can show a per-component
    # breakdown (ex-showroom, insurance, registration, accessories, etc.) as well as
    # a master total.  We use the most recently published version that is still active.
    master_items = connection.execute(
        text(
            """
            SELECT
                pli.component_key,
                pli.standard_amount,
                plv.price_list_version_id,
                plv.version_no,
                plv.currency_code AS plan_currency
            FROM auditcore.price_list_items pli
            JOIN auditcore.price_list_versions plv
              ON plv.tenant_id = pli.tenant_id
             AND plv.price_list_version_id = pli.price_list_version_id
            WHERE pli.tenant_id=:tenant_id
              AND pli.product_sku_id=:product_sku_id
              AND plv.lifecycle_status='PUBLISHED'
              AND (
                  CAST(:price_list_id AS uuid) IS NULL
                  OR plv.price_list_id = CAST(:price_list_id AS uuid)
              )
            ORDER BY plv.effective_from DESC, plv.version_no DESC, pli.component_key
            LIMIT 100
            """
        ),
        {
            "tenant_id": tenant_id,
            "product_sku_id": product_sku_id,
            "price_list_id": str(sku_row["price_list_id"]) if sku_row["price_list_id"] else None,
        },
    ).mappings().all()

    if not master_items:
        return None

    # Group by version — take only rows belonging to the first (latest) version.
    first_version_id = str(master_items[0]["price_list_version_id"])
    active_items = [r for r in master_items if str(r["price_list_version_id"]) == first_version_id]

    # An OEM price row carries both REGISTRATION_INDIVIDUAL and REGISTRATION_CORPORATE;
    # only the one that matches the buyer's basis counts toward the master total.
    _basis = registration_basis(
        customer_type_code=reviewed_booking.get("customer_type"),
        registration_type_code=reviewed_booking.get("registration_type"),
    )
    _skip_reg = "REGISTRATION_CORPORATE" if _basis == "INDIVIDUAL" else "REGISTRATION_INDIVIDUAL"

    master_components: list[dict[str, Any]] = []
    master_total = Decimal(0)
    currency = str(active_items[0]["plan_currency"]).upper()
    for item in active_items:
        component_key = str(item["component_key"]).strip().upper()
        if component_key == _skip_reg:
            continue
        amount = _to_decimal(item["standard_amount"])
        if amount is None:
            continue
        master_total += amount
        master_components.append({
            "componentKey": component_key,
            "masterAmount": float(amount),
            "currencyCode": currency,
        })

    # ---------- Booking-side amounts from reviewed Booking Form evidence ----------
    # Field names match the keys written by uc03_review_value_normalization to
    # booking_form_review_values.
    booking_ex_showroom = _to_decimal(reviewed_booking.get("ex_showroom_price"))
    booking_insurance   = _to_decimal(reviewed_booking.get("insurance_amount"))
    booking_registration = _to_decimal(reviewed_booking.get("registration_charges"))
    booking_road_tax    = _to_decimal(reviewed_booking.get("road_tax_amount"))
    booking_tcs         = _to_decimal(reviewed_booking.get("tcs_amount"))
    booking_rsa         = _to_decimal(reviewed_booking.get("rsa_amount"))
    booking_warranty    = _to_decimal(reviewed_booking.get("additional_warranty_amount"))
    booking_accessories = _to_decimal(reviewed_booking.get("accessories_cost"))
    booking_other       = _to_decimal(reviewed_booking.get("other_charges"))
    booking_discount    = _to_decimal(reviewed_booking.get("discount_amount"))
    booking_bonus       = _to_decimal(reviewed_booking.get("bonus_amount"))
    booking_total_price = _to_decimal(reviewed_booking.get("total_price"))
    booking_net_amount  = _to_decimal(reviewed_booking.get("net_amount"))

    # Enrich master_components with the matching booking-side amount.
    # The OEM price list keys components as EX_SHOWROOM / INSURANCE /
    # REGISTRATION_INDIVIDUAL / ... ; the reviewed booking uses ex_showroom_price
    # / insurance_amount / registration_charges / ... — bridged by
    # uc03_masters_alignment (deterministic, explicit).
    _BOOKING_COMPONENT_MAP: dict[str, Decimal | None] = {
        "ex_showroom_price": booking_ex_showroom,
        "insurance_amount":  booking_insurance,
        "registration_charges": booking_registration,
        "road_tax_amount":   booking_road_tax,
        "tcs_amount":        booking_tcs,
        "rsa_amount":        booking_rsa,
        "additional_warranty_amount": booking_warranty,
        "accessories_cost":  booking_accessories,
        "other_charges":     booking_other,
        "fastag_amount":     _to_decimal(reviewed_booking.get("fastag_amount")),
    }
    for component in master_components:
        oem_key = component["componentKey"]
        commercial_key = commercial_key_for_price_component(oem_key, basis=_basis)
        booking_val = _BOOKING_COMPONENT_MAP.get(commercial_key) if commercial_key else None
        if booking_val is not None:
            component["bookingAmount"] = float(booking_val)
            master_val = Decimal(str(component["masterAmount"]))
            dev = booking_val - master_val
            component["deviationAmount"] = float(dev.quantize(Decimal("0.01")))
            component["deviationPercent"] = (
                float((dev / master_val * Decimal(100)).quantize(Decimal("0.01")))
                if master_val != 0
                else None
            )
        else:
            component["bookingAmount"] = None
            component["deviationAmount"] = None
            component["deviationPercent"] = None

    # Top-level total deviation (master_total vs booking total_price)
    total_deviation: float | None = None
    total_deviation_pct: float | None = None
    if booking_total_price is not None and master_total != 0:
        dev_total = booking_total_price - master_total
        total_deviation = float(dev_total.quantize(Decimal("0.01")))
        total_deviation_pct = float(
            (dev_total / master_total * Decimal(100)).quantize(Decimal("0.01"))
        )

    return {
        "skuCode": str(sku_row["sku_code"]),
        "modelName": sku_row["model_name"],
        "variantName": sku_row["variant_name"],
        "colourName": sku_row["colour_name"],
        "selectionStatus": str(sku_row["selection_status"]),
        "selectionMethod": str(sku_row["selection_method"]) if sku_row["selection_method"] else None,
        "priceListVersionId": first_version_id,
        "currencyCode": currency,
        # Master totals
        "masterTotalAmount": float(master_total),
        "masterComponents": master_components,
        # Booking totals (from Booking Form DI evidence)
        "bookingTotalPrice": float(booking_total_price) if booking_total_price is not None else None,
        "bookingNetAmount": float(booking_net_amount) if booking_net_amount is not None else None,
        "bookingExShowroom": float(booking_ex_showroom) if booking_ex_showroom is not None else None,
        "bookingDiscount": float(booking_discount) if booking_discount is not None else None,
        "bookingBonus": float(booking_bonus) if booking_bonus is not None else None,
        # Deviation summary (master total vs booking total)
        "totalDeviationAmount": total_deviation,
        "totalDeviationPercent": total_deviation_pct,
    }


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
    """Project Journey 360 from Audit Core, including every reviewed DI field."""

    # Self-heal on read: resolve the SKU against the OEM price masters (or raise
    # MODEL_NOT_IDENTIFIED) so the Deal panel and findings below reflect it.
    set_tenant_context(connection, tenant_id)
    sync_model_resolution(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=""
    )
    reconcile_payments(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=""
    )

    base = legacy.get_journey_overview(
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
    )
    data = base.model_dump()
    data["discounts"] = _collapse_discounts(data.get("discounts") or [])
    review_statuses = _stage_review_statuses(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )

    journey = dict(data["journey"])
    journey["bookingPcVerificationStatus"] = review_statuses.get("BOOKING")
    journey["deliveryPcVerificationStatus"] = review_statuses.get("DELIVERY")
    data["journey"] = journey

    # Read the complete reviewed-field set from Audit Core, never directly from DI.
    # The resolver marks KYC as customer truth and Delivery as the winner over
    # Booking for every other overlapping semantic fact while retaining all sources.
    raw_reviewed_fields = reviewed_details.load_reviewed_field_details(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    reviewed_fields, resolved_reviewed = (
        reviewed_details.annotate_and_resolve_reviewed_fields(raw_reviewed_fields)
    )
    full_contact = legacy._can_read_full_contact(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    reviewed_details.mask_contact_fields(
        reviewed_fields,
        resolved_reviewed,
        full_contact=full_contact,
    )
    data["reviewedFields"] = reviewed_fields
    data["resolvedReviewedValues"] = resolved_reviewed

    reviewed_booking_rows = _reviewed_booking_rows(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    reviewed_booking = _reviewed_booking_projection(reviewed_booking_rows)

    customer = dict(data["customer"])
    resolved_customer_name = resolved_reviewed.get("customer_name")
    if (
        resolved_customer_name
        and str(resolved_customer_name.get("documentTypeKey") or "").casefold()
        in reviewed_details.KYC_DOCUMENT_TYPES
        and resolved_customer_name.get("value") not in (None, "")
    ):
        customer["legalName"] = resolved_customer_name["value"]
        customer["legalNameStatus"] = "DOCUMENT_VERIFIED"
    elif not customer.get("legalName"):
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

    # Add the resolved KYC detail to the Customer projection so the primary card
    # and the full reviewed-data section tell the same story.
    customer_identity_fields = {
        "dateOfBirth": "customer_date_of_birth",
        "gender": "customer_gender",
        "address": "customer_address",
        "panNumber": "pan",
        "aadhaarNumber": "aadhaar_number",
        "relationshipType": "customer_relationship_type",
        "relationshipName": "customer_relationship_name",
        "pincode": "pincode",
        "kycState": "kyc_state",
        "kycDistrict": "kyc_district",
    }
    for destination, semantic in customer_identity_fields.items():
        value = _resolved_value(resolved_reviewed, semantic)
        if value is not None:
            customer[destination] = value
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

    # Vehicle/product facts can legitimately be re-established by Delivery
    # documents. Use the resolved reviewed value even when Booking already had a
    # value, because Delivery is the user's declared final-stage precedence.
    for destination, semantic in (
        ("modelName", "model"),
        ("variantName", "variant"),
        ("colourName", "color"),
    ):
        value = _resolved_value(resolved_reviewed, semantic)
        if value is not None:
            booking[destination] = value

    booking["reviewedValues"] = reviewed_booking
    data["booking"] = booking or None

    # Preserve the established JourneyOverview payments contract. V2 receipt detail
    # is additive in the dedicated receipts collection below, so existing consumers
    # do not gain or lose fields unexpectedly.
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
    data["skuPricing"] = _sku_pricing_panel(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        reviewed_booking=reviewed_booking,
    )
    data["bankStatementLines"] = _bank_statement_lines(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    data["invoices"] = _invoices(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    data["scrappageCertificates"] = _scrappage_certificates(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    return JourneyOverviewProjectionResponse.model_validate(data)
