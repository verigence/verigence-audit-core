"""Governed UC03 Phase 2 document blueprint.

This module is the executable representation of the 29-document P2 design
baseline. It does not change DI or legacy UC03 configuration. The worker may
only use an entry as executable after its live-contract validation has passed.

The baseline intentionally distinguishes:
- DEDICATED: a document-specific DI schema exists;
- GENERALIZED_INVOICE: a named document type is backed by DI's common invoice
  evidence superset;
- FALLBACK: no dedicated DI schema exists; generic retention/review remains valid.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Stage = Literal["BOOKING", "DELIVERY"]
RequirementLevel = Literal["REQUIRED", "CONDITIONAL", "OPTIONAL"]
DiSchemaMode = Literal["DEDICATED", "GENERALIZED_INVOICE", "FALLBACK"]


@dataclass(frozen=True)
class P2DocumentBlueprint:
    document_type_key: str
    stage: Stage
    requirement_level: RequirementLevel
    condition_key: str | None
    di_schema_mode: DiSchemaMode
    typed_store: str
    processors: tuple[str, ...]
    controls: tuple[str, ...]
    journey_360_sections: tuple[str, ...]
    task_effects: tuple[str, ...]


def _bp(
    key: str,
    stage: Stage,
    level: RequirementLevel,
    mode: DiSchemaMode,
    typed_store: str,
    *,
    condition: str | None = None,
    processors: tuple[str, ...] = (),
    controls: tuple[str, ...] = (),
    journey: tuple[str, ...] = (),
    tasks: tuple[str, ...] = (),
) -> P2DocumentBlueprint:
    return P2DocumentBlueprint(
        document_type_key=key,
        stage=stage,
        requirement_level=level,
        condition_key=condition,
        di_schema_mode=mode,
        typed_store=typed_store,
        processors=processors,
        controls=controls,
        journey_360_sections=journey,
        task_effects=tasks,
    )


P2_DOCUMENT_BLUEPRINT: dict[str, P2DocumentBlueprint] = {
    "booking_form": _bp(
        "booking_form", "BOOKING", "REQUIRED", "DEDICATED",
        "booking_form_review_values",
        processors=(
            "BOOKING_MATERIALIZATION", "SKU_RESOLUTION",
            "BOOKING_INTIMATION", "DEAL_RECONCILIATION",
        ),
        controls=(
            "MODEL_NOT_IDENTIFIED", "BK_DOCKET_PRESENT",
            "BK_DISCOUNT_EVIDENCE_MISSING", "WRONG_DOCUMENT",
            "DUPLICATE_BOOKING",
        ),
        journey=("CUSTOMER", "BOOKING", "VEHICLE", "DEAL", "DISCOUNTS", "ADD_ONS"),
        tasks=(
            "LOW_CONFIDENCE_REVIEW", "WRONG_DOCUMENT_REVIEW",
            "CORRECTION_REVIEW", "REUPLOAD_WHEN_REJECTED",
        ),
    ),
    "pan_card": _bp(
        "pan_card", "BOOKING", "OPTIONAL", "DEDICATED",
        "customer_identity_review_values",
        processors=("IDENTITY_MATERIALIZATION", "SOURCE_RESOLUTION"),
        controls=("WRONG_DOCUMENT", "DUPLICATE_BOOKING", "BK_PAN_PRESENT"),
        journey=("CUSTOMER_KYC",),
        tasks=("LOW_CONFIDENCE_REVIEW", "WRONG_DOCUMENT_REVIEW", "FIELD_CORRECTION"),
    ),
    "aadhaar": _bp(
        "aadhaar", "BOOKING", "OPTIONAL", "DEDICATED",
        "customer_identity_review_values",
        processors=("IDENTITY_MATERIALIZATION", "AADHAAR_ADDRESS_OWNERSHIP"),
        controls=("WRONG_DOCUMENT", "DUPLICATE_BOOKING", "BK_PAN_PRESENT"),
        journey=("CUSTOMER_KYC",),
        tasks=("LOW_CONFIDENCE_REVIEW", "WRONG_DOCUMENT_REVIEW", "FIELD_CORRECTION"),
    ),
    "dealer_receipt": _bp(
        "dealer_receipt", "BOOKING", "REQUIRED", "DEDICATED",
        "dealer_receipt_review_values",
        processors=(
            "RECEIPT_MATERIALIZATION", "DUPLICATE_DETECTION",
            "PAYMENT_RECONCILIATION", "MINIMUM_BOOKING_EVALUATION",
            "FINANCE_CANDIDATE_INPUT",
        ),
        controls=(
            "DUPLICATE_RECEIPT", "WRONG_DOCUMENT", "PAYMENT_BANK_UNMATCHED",
            "BK_MIN_BOOKING_AMOUNT_NOT_MET", "BK_MIN_BOOKING_PROOF_PRESENT",
        ),
        journey=("PAYMENTS", "BOOKING", "FINANCE"),
        tasks=(
            "LOW_CONFIDENCE_REVIEW", "DUPLICATE_RECEIPT_NOTICE",
            "WRONG_DOCUMENT_REVIEW", "REUPLOAD_DOCUMENT",
        ),
    ),
    "customer_kyc": _bp(
        "customer_kyc", "BOOKING", "REQUIRED", "DEDICATED",
        "generic_reviewed_field_store",
        processors=("IDENTITY_CONSISTENCY", "DUPLICATE_BOOKING_INPUTS"),
        controls=("WRONG_DOCUMENT", "DUPLICATE_BOOKING", "BK_REQUIRED_CAPTURE_COMPLETE"),
        journey=("CUSTOMER_KYC",),
        tasks=("LOW_CONFIDENCE_REVIEW", "WRONG_DOCUMENT_REVIEW", "FIELD_CORRECTION"),
    ),
    "gst_certificate": _bp(
        "gst_certificate", "DELIVERY", "CONDITIONAL", "DEDICATED",
        "generic_reviewed_field_store",
        condition="corporateCustomer",
        processors=("REQUIREMENT_APPLICABILITY", "CORPORATE_EVIDENCE"),
        controls=("DELIVERY_COMPLETENESS", "EXTERNAL_CORPORATE_GST_NAME"),
        journey=("DOCUMENTS", "CORPORATE_EVIDENCE", "DEAL_AUDIT"),
        tasks=("LOW_CONFIDENCE_REVIEW", "REUPLOAD_DOCUMENT", "PROVIDE_FEEDBACK"),
    ),
    "corporate_id": _bp(
        "corporate_id", "DELIVERY", "CONDITIONAL", "DEDICATED",
        "generic_reviewed_field_store",
        condition="corporateCustomer",
        processors=("SUPPORTING_EVIDENCE", "SOURCE_FACTS"),
        controls=("BK_DISCOUNT_EVIDENCE_MISSING",),
        journey=("DOCUMENTS", "DISCOUNTS_EVIDENCE"),
        tasks=("REUPLOAD_DOCUMENT", "LOW_CONFIDENCE_REVIEW"),
    ),
    "vehicle_rc": _bp(
        "vehicle_rc", "DELIVERY", "CONDITIONAL", "FALLBACK",
        "generic_reviewed_field_store",
        condition="exchangeTaken",
        processors=("TRADE_IN_SUPPORTING_EVIDENCE",),
        controls=("BK_DISCOUNT_EVIDENCE_MISSING", "DELIVERY_COMPLETENESS"),
        journey=("TRADE_IN", "DOCUMENTS", "DEAL_EVIDENCE"),
        tasks=("REUPLOAD_DOCUMENT", "REVIEW_DOCUMENT"),
    ),
    "transfer_letter": _bp(
        "transfer_letter", "DELIVERY", "OPTIONAL", "FALLBACK",
        "generic_reviewed_field_store",
        processors=("GENERIC_DOCUMENT_PROCESSING",),
        journey=("TRADE_IN", "DOCUMENTS"),
        tasks=("LOW_CONFIDENCE_REVIEW", "REVIEW_DOCUMENT"),
    ),
    "authorization_letter": _bp(
        "authorization_letter", "DELIVERY", "OPTIONAL", "FALLBACK",
        "generic_reviewed_field_store",
        processors=("GENERIC_DOCUMENT_PROCESSING",),
        journey=("TRADE_IN", "DOCUMENTS"),
        tasks=("LOW_CONFIDENCE_REVIEW", "REVIEW_DOCUMENT"),
    ),
    "wholesale_invoice": _bp(
        "wholesale_invoice", "DELIVERY", "REQUIRED", "GENERALIZED_INVOICE",
        "invoice_review_values",
        processors=("INVOICE_MATERIALIZATION", "COMMERCIAL_PROJECTION", "INVOICE_SKU_FALLBACK"),
        controls=("MODEL_NOT_IDENTIFIED", "WRONG_DOCUMENT", "INVOICE_RUNTIME_CONTROLS"),
        journey=("INVOICES", "DEAL", "VEHICLE", "FINANCE"),
        tasks=("LOW_CONFIDENCE_REVIEW", "WRONG_DOCUMENT_REVIEW", "INVOICE_DISCREPANCY_REVIEW"),
    ),
    "customer_invoice_dms": _bp(
        "customer_invoice_dms", "DELIVERY", "REQUIRED", "GENERALIZED_INVOICE",
        "invoice_review_values",
        processors=("INVOICE_MATERIALIZATION", "DEAL", "VEHICLE", "FINANCE", "SKU_FALLBACK_MISMATCH"),
        controls=(
            "WRONG_DOCUMENT", "DUPLICATE_BOOKING", "INVOICE_FIELD_DISAGREEMENT",
            "MODEL_NOT_IDENTIFIED", "INVOICE_SKU_MISMATCH", "EXTERNAL_INVOICE_CONTROLS",
        ),
        journey=("INVOICES", "DEAL", "VEHICLE", "FINANCE", "CUSTOMER"),
        tasks=(
            "LOW_CONFIDENCE_REVIEW", "WRONG_DOCUMENT_REVIEW",
            "INVOICE_DISCREPANCY_REVIEW", "FIELD_CORRECTION", "REUPLOAD_DOCUMENT",
        ),
    ),
    "tax_invoice_tally": _bp(
        "tax_invoice_tally", "DELIVERY", "REQUIRED", "GENERALIZED_INVOICE",
        "invoice_review_values",
        processors=("INVOICE_MATERIALIZATION", "COMMERCIAL_PROJECTION", "SKU_FALLBACK_MISMATCH"),
        controls=("INVOICE_FIELD_DISAGREEMENT", "INVOICE_SKU_MISMATCH", "EXTERNAL_DMS_VS_TALLY"),
        journey=("INVOICES", "DEAL", "VEHICLE"),
        tasks=("LOW_CONFIDENCE_REVIEW", "INVOICE_DISCREPANCY_REVIEW", "FIELD_CORRECTION"),
    ),
    "insurance_cover": _bp(
        "insurance_cover", "DELIVERY", "REQUIRED", "DEDICATED",
        "insurance_records",
        processors=("INSURANCE_MATERIALIZATION", "IDENTITY_DEAL_INPUTS"),
        controls=("WRONG_DOCUMENT", "EXTERNAL_INSURANCE_COMPARISONS"),
        journey=("INSURANCE", "DEAL", "VEHICLE_EVIDENCE"),
        tasks=("LOW_CONFIDENCE_REVIEW", "WRONG_DOCUMENT_REVIEW", "FIELD_CORRECTION", "PROVIDE_FEEDBACK"),
    ),
    "accessory_invoice_dms": _bp(
        "accessory_invoice_dms", "DELIVERY", "REQUIRED", "GENERALIZED_INVOICE",
        "invoice_review_values",
        processors=("INVOICE_ADDON_MATERIALIZATION", "DEAL", "VEHICLE"),
        controls=("INVOICE_DISCREPANCY", "EXTERNAL_ACCESSORY_COMPARISONS"),
        journey=("INVOICES", "ACCESSORIES", "DEAL", "VEHICLE"),
        tasks=("LOW_CONFIDENCE_REVIEW", "INVOICE_DISCREPANCY_REVIEW", "FIELD_CORRECTION"),
    ),
    "accessory_invoice_tally": _bp(
        "accessory_invoice_tally", "DELIVERY", "REQUIRED", "GENERALIZED_INVOICE",
        "invoice_review_values",
        processors=("INVOICE_ADDON_MATERIALIZATION",),
        controls=("EXTERNAL_ACCESSORY_TALLY_VS_DMS",),
        journey=("INVOICES", "ACCESSORIES", "DEAL"),
        tasks=("LOW_CONFIDENCE_REVIEW", "INVOICE_DISCREPANCY_REVIEW", "FIELD_CORRECTION"),
    ),
    "rto_challan": _bp(
        "rto_challan", "DELIVERY", "REQUIRED", "DEDICATED",
        "generic_reviewed_field_store",
        processors=("REGISTRATION", "RTO_COMMERCIAL_LINES", "FINANCE_HYPOTHECATION"),
        controls=("FINANCE_HYPOTHECATION_MISSING", "DELIVERY_COMPLETENESS", "EXTERNAL_RTO_COMPLETENESS"),
        journey=("REGISTRATION", "FINANCE", "DEAL"),
        tasks=("LOW_CONFIDENCE_REVIEW", "FIELD_CORRECTION", "ADD_EVIDENCE"),
    ),
    "customer_ledger": _bp(
        "customer_ledger", "DELIVERY", "REQUIRED", "DEDICATED",
        "generic_reviewed_field_store",
        processors=("COMMERCIAL_SOURCE_MATERIALIZATION",),
        controls=("EXTERNAL_INVOICE_VS_LEDGER", "EXTERNAL_PAYMENT_VS_LEDGER"),
        journey=("DEAL", "PAYMENTS"),
        tasks=("LOW_CONFIDENCE_REVIEW", "PROVIDE_FEEDBACK", "ADD_EVIDENCE"),
    ),
    "cost_sheet": _bp(
        "cost_sheet", "DELIVERY", "REQUIRED", "FALLBACK",
        "generic_reviewed_field_store",
        processors=("COMMERCIAL_SOURCE_MATERIALIZATION",),
        controls=("EXTERNAL_RECONCILIATION_COMPARISONS",),
        journey=("DEAL",),
        tasks=("REVIEW_DOCUMENT", "FIELD_CORRECTION"),
    ),
    "gate_pass": _bp(
        "gate_pass", "DELIVERY", "REQUIRED", "DEDICATED",
        "generic_reviewed_field_store",
        processors=("DELIVERY_DATE_MATERIALIZATION",),
        controls=("DELIVERY_DATE_BEFORE_BOOKING_DATE", "EXTERNAL_DELIVERY_DATE_CHASSIS"),
        journey=("DELIVERY", "VEHICLE_EVIDENCE", "AUDIT"),
        tasks=("LOW_CONFIDENCE_REVIEW", "FIELD_CORRECTION", "RULE_REMEDIATION"),
    ),
    "ew_invoice": _bp(
        "ew_invoice", "DELIVERY", "REQUIRED", "GENERALIZED_INVOICE",
        "invoice_review_values",
        processors=("INVOICE_ADDON_MATERIALIZATION",),
        controls=("INVOICE_DISCREPANCY", "EXTERNAL_ADDON_COMPARISONS"),
        journey=("INVOICES", "ADD_ONS", "DEAL"),
        tasks=("LOW_CONFIDENCE_REVIEW", "INVOICE_DISCREPANCY_REVIEW", "FIELD_CORRECTION"),
    ),
    "rsa_invoice": _bp(
        "rsa_invoice", "DELIVERY", "REQUIRED", "GENERALIZED_INVOICE",
        "invoice_review_values",
        processors=("INVOICE_ADDON_MATERIALIZATION",),
        controls=("INVOICE_DISCREPANCY", "EXTERNAL_ADDON_COMPARISONS"),
        journey=("INVOICES", "ADD_ONS", "DEAL"),
        tasks=("LOW_CONFIDENCE_REVIEW", "INVOICE_DISCREPANCY_REVIEW", "FIELD_CORRECTION"),
    ),
    "value_added_service_document": _bp(
        "value_added_service_document", "DELIVERY", "OPTIONAL", "FALLBACK",
        "generic_reviewed_field_store",
        processors=("GENERIC_DOCUMENT_PROCESSING",),
        journey=("DOCUMENTS", "EVIDENCE"),
        tasks=("REVIEW_DOCUMENT", "FIELD_CORRECTION"),
    ),
    "no_dues_certificate": _bp(
        "no_dues_certificate", "DELIVERY", "REQUIRED", "FALLBACK",
        "generic_reviewed_field_store",
        processors=("GENERIC_DOCUMENT_PROCESSING",),
        controls=("DELIVERY_COMPLETENESS",),
        journey=("DOCUMENTS", "DELIVERY_EVIDENCE"),
        tasks=("UPLOAD_DOCUMENT", "REUPLOAD_DOCUMENT", "REVIEW_DOCUMENT"),
    ),
    "payment_receipt": _bp(
        "payment_receipt", "DELIVERY", "REQUIRED", "DEDICATED",
        "dealer_receipt_review_values",
        processors=("RECEIPT_MATERIALIZATION", "DUPLICATE_DETECTION", "PAYMENT_RECONCILIATION", "FINANCE_CANDIDATE_INPUT"),
        controls=("DUPLICATE_RECEIPT", "WRONG_DOCUMENT", "PAYMENT_BANK_UNMATCHED", "EXTERNAL_RECEIPT_SUM_RULES"),
        journey=("PAYMENTS", "FINANCE", "DEAL"),
        tasks=("LOW_CONFIDENCE_REVIEW", "DUPLICATE_RECEIPT_NOTICE", "WRONG_DOCUMENT_REVIEW", "REUPLOAD_DOCUMENT"),
    ),
    "bank_statement_extract": _bp(
        "bank_statement_extract", "DELIVERY", "OPTIONAL", "DEDICATED",
        "bank_statement_lines",
        processors=("BANK_STATEMENT_MATERIALIZATION", "PAYMENT_RECONCILIATION"),
        controls=("PAYMENT_BANK_UNMATCHED",),
        journey=("PAYMENTS", "RECONCILIATION"),
        tasks=("LOW_CONFIDENCE_REVIEW", "FIELD_CORRECTION"),
    ),
    "credit_note": _bp(
        "credit_note", "DELIVERY", "OPTIONAL", "GENERALIZED_INVOICE",
        "invoice_review_values",
        processors=("INVOICE_CREDIT_NOTE_MATERIALIZATION",),
        controls=("INVOICE_DISCREPANCY", "DISCOUNT_RECONCILIATION"),
        journey=("INVOICES", "DISCOUNTS", "DEAL"),
        tasks=("LOW_CONFIDENCE_REVIEW", "INVOICE_DISCREPANCY_REVIEW", "FIELD_CORRECTION"),
    ),
    "gst_declaration": _bp(
        "gst_declaration", "DELIVERY", "OPTIONAL", "DEDICATED",
        "generic_reviewed_field_store",
        processors=("GENERIC_DOCUMENT_PROCESSING",),
        journey=("DOCUMENTS", "TAX_EVIDENCE"),
        tasks=("REVIEW_DOCUMENT", "FIELD_CORRECTION"),
    ),
    "scrappage_certificate_of_deposit": _bp(
        "scrappage_certificate_of_deposit", "DELIVERY", "OPTIONAL", "DEDICATED",
        "scrappage_certificate_review_values",
        processors=("SCRAPPAGE_MATERIALIZATION", "CONDITIONAL_EVIDENCE_SATISFACTION"),
        controls=("BK_DISCOUNT_EVIDENCE_MISSING",),
        journey=("SCRAPPAGE", "TRADE_IN", "DEAL_DISCOUNTS"),
        tasks=("UPLOAD_DOCUMENT", "REUPLOAD_DOCUMENT", "LOW_CONFIDENCE_REVIEW", "FIELD_CORRECTION"),
    ),
}

P2_DOCUMENT_TYPES = frozenset(P2_DOCUMENT_BLUEPRINT)
P2_FALLBACK_DOCUMENT_TYPES = frozenset(
    key for key, entry in P2_DOCUMENT_BLUEPRINT.items()
    if entry.di_schema_mode == "FALLBACK"
)
P2_GENERALIZED_INVOICE_TYPES = frozenset(
    key for key, entry in P2_DOCUMENT_BLUEPRINT.items()
    if entry.di_schema_mode == "GENERALIZED_INVOICE"
)
P2_DEDICATED_DI_TYPES = frozenset(
    key for key, entry in P2_DOCUMENT_BLUEPRINT.items()
    if entry.di_schema_mode == "DEDICATED"
)


def get_p2_document_blueprint(document_type_key: str | None) -> P2DocumentBlueprint | None:
    return P2_DOCUMENT_BLUEPRINT.get(str(document_type_key or "").strip().lower())
