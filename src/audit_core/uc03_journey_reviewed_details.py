from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_attribute_mapping import spec_for_field

# Journey Details precedence is deliberately simple and explicit:
# 1. PAN/Aadhaar are the customer identity source of truth.
# 2. For every other overlapping business fact, reviewed Delivery evidence wins
#    over reviewed Booking evidence.
# 3. No reviewed DI field is discarded: non-winning source values remain visible.
KYC_DOCUMENT_TYPES = frozenset({"pan", "pan_card", "aadhaar", "address_proof"})

_CUSTOMER_SEMANTIC_KEYS = frozenset(
    {
        "customer_name",
        "customer_date_of_birth",
        "aadhaar_number",
        "pan",
        "customer_gender",
        "customer_address",
        "pincode",
        "kyc_state",
        "kyc_district",
        "customer_relationship_type",
        "customer_relationship_name",
    }
)

# Exact DI field aliases that represent the same Journey business fact.  This is
# not fuzzy label matching: every alias below is a technical field key emitted by
# an approved/observed DI schema or already used by Audit Core UC03 mappings.
_FIELD_SEMANTIC_ALIASES: dict[str, str] = {
    "pan_name": "customer_name",
    "aadhaar_name": "customer_name",
    "customer_name": "customer_name",
    "date_of_birth": "customer_date_of_birth",
    "pan_number": "pan",
    "aadhaar_number": "aadhaar_number",
    "gender": "customer_gender",
    "aadhaar_address": "customer_address",
    "address_principal_place": "customer_address",
    "address_pincode": "pincode",
    "address_state": "kyc_state",
    "address_district": "kyc_district",
    "pan_relationship_type": "customer_relationship_type",
    "aadhaar_relationship_type": "customer_relationship_type",
    "pan_relationship_name": "customer_relationship_name",
    "aadhaar_relationship_name": "customer_relationship_name",
    "model_name_raw": "model",
    "vehicle_model": "model",
    "vehicle_model_name": "model",
    "variant_raw": "variant",
    "vehicle_variant": "variant",
    "vehicle_color": "color",
    "vin": "vin",
    "vin_number": "vin",
    "chassis_no": "chassis_number",
    "chassis_number": "chassis_number",
    "vehicle_registration_number": "registration_number",
    "registration_number": "registration_number",
    "insured_vehicle_reg": "registration_number",
    "invoice_number": "invoice_reference",
    "dms_invoice_number": "invoice_reference",
    "policy_number": "policy_reference",
    "premium_amount": "insurance_actual_amount",
    "gstin": "gstin",
}

_PAYMENT_DOCUMENT_TYPES = frozenset(
    {
        "dealer_receipt",
        "payment_receipt",
        "bank_statement",
        "upi_receipt",
        "cheque",
    }
)
_INSURANCE_DOCUMENT_TYPES = frozenset(
    {"insurance_cover", "insurance_cover_note", "insurance_policy"}
)
_INVOICE_DOCUMENT_TYPES = frozenset(
    {
        "customer_invoice_dms",
        "tax_invoice_dms",
        "tax_invoice_tally",
        "tax_invoice",
        "wholesale_invoice",
        "invoice_generic",
    }
)
_ACCESSORY_DOCUMENT_TYPES = frozenset(
    {"accessory_invoice_dms", "accessory_invoice_tally", "accessory_invoice"}
)
_EXTENDED_WARRANTY_DOCUMENT_TYPES = frozenset(
    {"ew_invoice", "extended_warranty_invoice", "extended_warranty"}
)
_RSA_DOCUMENT_TYPES = frozenset({"rsa_invoice", "rsa"})
_TRADE_IN_DOCUMENT_TYPES = frozenset(
    {
        "trade_in_rc",
        "trade_in_valuation",
        "old_vehicle_rc",
        "rc_transfer_letter",
        "transfer_letter",
        "authorization_letter",
    }
)
_FINANCE_DOCUMENT_TYPES = frozenset(
    {"bank_approval_letter", "bank_do", "delivery_order", "finance_approval"}
)
_REGISTRATION_DOCUMENT_TYPES = frozenset(
    {"rto_challan", "registration_invoice", "registration_document"}
)
_DELIVERY_DOCUMENT_TYPES = frozenset(
    {
        "gate_pass",
        "delivery_order_cover",
        "no_dues_certificate",
        "ndc",
        "customer_ledger",
        "cost_sheet",
        "docket_audit_form",
    }
)
_BOOKING_DOCUMENT_TYPES = frozenset({"booking_form", "booking_docket"})


def semantic_key(field_key: str) -> str:
    normalized = str(field_key or "").strip().casefold()
    if not normalized:
        return "unknown"
    if normalized in _FIELD_SEMANTIC_ALIASES:
        return _FIELD_SEMANTIC_ALIASES[normalized]
    spec = spec_for_field(normalized)
    return spec.attribute_key if spec is not None else normalized


def business_category(document_type_key: str | None, field_key: str) -> str:
    document_type = str(document_type_key or "").strip().casefold()
    normalized_field = str(field_key or "").strip().casefold()

    if document_type in KYC_DOCUMENT_TYPES:
        return "CUSTOMER"
    if document_type in _PAYMENT_DOCUMENT_TYPES:
        return "PAYMENTS"
    if document_type in _INSURANCE_DOCUMENT_TYPES:
        return "INSURANCE"
    if document_type == "gst_certificate":
        return "GST"
    if document_type in {"corporate_id", "corporate_id_card", "corporate_certificate"}:
        return "CORPORATE"
    if document_type in _ACCESSORY_DOCUMENT_TYPES:
        return "ACCESSORIES"
    if document_type in _EXTENDED_WARRANTY_DOCUMENT_TYPES:
        return "EXTENDED_WARRANTY"
    if document_type in _RSA_DOCUMENT_TYPES:
        return "RSA"
    if document_type in _TRADE_IN_DOCUMENT_TYPES:
        return "TRADE_IN"
    if document_type in _FINANCE_DOCUMENT_TYPES:
        return "FINANCE"
    if document_type in _REGISTRATION_DOCUMENT_TYPES:
        return "REGISTRATION"
    if document_type in _DELIVERY_DOCUMENT_TYPES:
        return "DELIVERY"
    if document_type in _INVOICE_DOCUMENT_TYPES:
        return "VEHICLE_INVOICE"
    if document_type in _BOOKING_DOCUMENT_TYPES:
        return "BOOKING"

    # A few invoice-derived product families carry their category in the field
    # rather than the document type. Keep those useful groupings even for tenant
    # aliases that DI may classify differently.
    if normalized_field in {"plan_name", "coverage_start_date", "coverage_end_date", "tenure_months"}:
        if "warranty" in document_type or document_type.startswith("ew_"):
            return "EXTENDED_WARRANTY"
        if "rsa" in document_type:
            return "RSA"
    return "OTHER"


def load_reviewed_field_details(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    """Load the latest Audit Core copy of every reviewed DI field for a Journey.

    This function never calls DI. It reads only Audit Core's durable reviewed-field
    store and enriches it with the captured document metadata already owned by Core.
    Rejected fields are intentionally returned too, because Journey Details must
    preserve what DI extracted even when the reviewer did not accept it as final.
    """

    rows = connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    f.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            f.stage_code,
                            f.di_document_id,
                            COALESCE(
                                f.source_canonical_field_id,
                                f.source_fact_ref::text,
                                f.field_key
                            )
                        ORDER BY
                            f.source_fact_version DESC,
                            f.reviewed_at_utc DESC NULLS LAST,
                            f.updated_at_utc DESC,
                            f.extracted_field_id DESC
                    ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id=:tenant_id
                  AND f.journey_id=:journey_id
                  AND f.stage_code IN ('BOOKING', 'DELIVERY')
            )
            SELECT
                r.extracted_field_id AS "reviewedFieldId",
                r.di_document_id AS "documentId",
                r.evidence_id AS "evidenceId",
                r.stage_code AS "stageCode",
                COALESCE(
                    r.source_document_type_key,
                    d.classified_document_type_key,
                    e.document_type_key
                ) AS "documentTypeKey",
                d.requirement_key AS "requirementKey",
                d.original_filename AS "originalFilename",
                r.source_canonical_field_id AS "canonicalFieldId",
                r.field_key AS "fieldKey",
                r.extracted_value AS "extractedValue",
                r.modified_value AS "modifiedValue",
                r.effective_value AS "effectiveValue",
                (r.effective_value IS NOT NULL) AS "hasEffectiveValue",
                r.is_modified AS "isModified",
                r.confidence_score AS "confidenceScore",
                r.confidence_scale AS "confidenceScale",
                r.source_fact_version AS "sourceFactVersion",
                r.reviewed_by_actor_id AS "reviewedByActorId",
                r.reviewed_at_utc AS "reviewedAtUtc"
            FROM ranked r
            LEFT JOIN auditcore.document_capture_v2_documents d
              ON d.tenant_id=r.tenant_id
             AND d.journey_id=r.journey_id
             AND d.di_document_id=r.di_document_id
            LEFT JOIN auditcore.evidence e
              ON e.tenant_id=r.tenant_id
             AND e.journey_id=r.journey_id
             AND e.evidence_id=r.evidence_id
            WHERE r.row_rank=1
            ORDER BY
                CASE r.stage_code WHEN 'DELIVERY' THEN 0 ELSE 1 END,
                COALESCE(r.source_document_type_key, d.classified_document_type_key, e.document_type_key, ''),
                r.di_document_id,
                r.field_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _source_priority_rank(item: dict[str, Any]) -> int:
    spec = spec_for_field(str(item.get("fieldKey") or ""))
    if spec is None:
        return 999
    document_type = str(item.get("documentTypeKey") or "").strip().casefold()
    for index, source in enumerate(spec.source_priority):
        if document_type == source.casefold():
            return index
    return len(spec.source_priority) + 1


def _kyc_rank(item: dict[str, Any], semantic: str) -> int:
    document_type = str(item.get("documentTypeKey") or "").strip().casefold()
    # PAN is the deterministic tie-break for shared legal identity facts. Aadhaar
    # is preferred for Aadhaar-specific/address facts. Both remain source-of-truth
    # documents and every source value remains visible in the detail projection.
    if semantic in {"customer_name", "customer_date_of_birth", "pan", "customer_relationship_type", "customer_relationship_name"}:
        order = {"pan": 0, "pan_card": 0, "aadhaar": 1, "address_proof": 2}
    else:
        order = {"aadhaar": 0, "address_proof": 1, "pan": 2, "pan_card": 2}
    return order.get(document_type, 99)


def _candidate_rank(item: dict[str, Any], semantic: str) -> tuple[Any, ...]:
    document_type = str(item.get("documentTypeKey") or "").strip().casefold()
    stage = str(item.get("stageCode") or "").strip().upper()
    fact_version = int(item.get("sourceFactVersion") or 0)

    if semantic in _CUSTOMER_SEMANTIC_KEYS:
        kyc = document_type in KYC_DOCUMENT_TYPES
        return (
            0 if kyc else 1,
            _kyc_rank(item, semantic) if kyc else 99,
            0 if stage == "DELIVERY" else 1,
            _source_priority_rank(item),
            -fact_version,
            str(item.get("documentId") or ""),
            str(item.get("reviewedFieldId") or ""),
        )

    return (
        0 if stage == "DELIVERY" else 1,
        _source_priority_rank(item),
        -fact_version,
        document_type,
        str(item.get("documentId") or ""),
        str(item.get("reviewedFieldId") or ""),
    )


def _precedence_reason(
    winner: dict[str, Any],
    candidates: list[dict[str, Any]],
    semantic: str,
) -> str:
    document_type = str(winner.get("documentTypeKey") or "").strip().casefold()
    stage = str(winner.get("stageCode") or "").strip().upper()
    if semantic in _CUSTOMER_SEMANTIC_KEYS and document_type in KYC_DOCUMENT_TYPES:
        return "CUSTOMER_KYC_SOURCE_OF_TRUTH"
    if stage == "DELIVERY" and any(
        str(candidate.get("stageCode") or "").strip().upper() == "BOOKING"
        for candidate in candidates
    ):
        return "DELIVERY_OVER_BOOKING"
    if len(candidates) == 1:
        return "ONLY_REVIEWED_SOURCE"
    return "DELIVERY_CURRENT_SOURCE" if stage == "DELIVERY" else "BOOKING_CURRENT_SOURCE"


def annotate_and_resolve_reviewed_fields(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Annotate every reviewed source row and resolve one preferred Journey value.

    The returned ``annotated`` collection is lossless with respect to the supplied
    rows. ``resolved`` is only the convenience projection used by Journey Details;
    it never deletes or rewrites the non-winning source facts.
    """

    annotated: list[dict[str, Any]] = []
    candidates_by_semantic: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        item = dict(row)
        semantic = semantic_key(str(item.get("fieldKey") or ""))
        item["semanticKey"] = semantic
        item["businessCategory"] = business_category(
            item.get("documentTypeKey"),
            str(item.get("fieldKey") or ""),
        )
        item["displayValue"] = (
            item.get("effectiveValue")
            if item.get("hasEffectiveValue")
            else item.get("extractedValue")
        )
        item["isPreferred"] = False
        item["precedenceReason"] = None
        annotated.append(item)
        if item.get("hasEffectiveValue"):
            candidates_by_semantic[semantic].append(item)

    resolved: dict[str, dict[str, Any]] = {}
    for semantic, candidates in candidates_by_semantic.items():
        winner = min(candidates, key=lambda item: _candidate_rank(item, semantic))
        reason = _precedence_reason(winner, candidates, semantic)
        winner["isPreferred"] = True
        winner["precedenceReason"] = reason
        resolved[semantic] = {
            "value": winner.get("effectiveValue"),
            "reviewedFieldId": winner.get("reviewedFieldId"),
            "documentId": winner.get("documentId"),
            "evidenceId": winner.get("evidenceId"),
            "documentTypeKey": winner.get("documentTypeKey"),
            "fieldKey": winner.get("fieldKey"),
            "stageCode": winner.get("stageCode"),
            "businessCategory": winner.get("businessCategory"),
            "sourceFactVersion": winner.get("sourceFactVersion"),
            "precedenceReason": reason,
        }

    return annotated, resolved


def mask_contact_fields(
    fields: list[dict[str, Any]],
    resolved: dict[str, dict[str, Any]],
    *,
    full_contact: bool,
) -> None:
    """Apply the existing Journey contact-visibility rule to generic DI detail."""

    if full_contact:
        return

    def mask(value: Any) -> str | None:
        if value is None:
            return None
        digits = "".join(character for character in str(value) if character.isdigit())
        return f"******{digits[-4:]}" if len(digits) >= 4 else None

    phone_semantics = {"customer_number", "contact_number", "customer_phone"}
    for item in fields:
        field_key = str(item.get("fieldKey") or "").strip().casefold()
        semantic = str(item.get("semanticKey") or "").strip().casefold()
        if "phone" not in field_key and semantic not in phone_semantics:
            continue
        for key in ("extractedValue", "modifiedValue", "effectiveValue", "displayValue"):
            item[key] = mask(item.get(key))

    for semantic, item in resolved.items():
        if "phone" in semantic.casefold() or semantic.casefold() in phone_semantics:
            item["value"] = mask(item.get("value"))
