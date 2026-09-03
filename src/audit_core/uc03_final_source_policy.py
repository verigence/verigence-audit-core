from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewedSourcePolicy:
    attribute_key: str
    report_field: str
    business_source_label: str
    technical_pairs: tuple[tuple[str, str], ...]
    resolution_rule: str


@dataclass(frozen=True)
class UnresolvedTechnicalPolicy:
    report_field: str
    business_source_label: str
    reason: str


# Only mappings that current Audit Core evidence establishes without relying on a
# disputed alias or an unverified DI field key belong here. Step 2 may add entries
# after DI contract validation; this module must never guess an alias.
PROVEN_REVIEWED_SOURCE_POLICIES: tuple[ReviewedSourcePolicy, ...] = (
    ReviewedSourcePolicy(
        attribute_key="customer_name",
        report_field="Customer Name",
        business_source_label="Customer KYC (PAN, Aadhaar, address proof)",
        technical_pairs=(("pan_card", "pan_name"), ("aadhaar", "aadhaar_name")),
        resolution_rule="FINAL_REPORT_CUSTOMER_KYC_UNANIMOUS",
    ),
    ReviewedSourcePolicy(
        attribute_key="aadhaar_number",
        report_field="Aadhar No",
        business_source_label="Customer KYC (PAN, Aadhaar, address proof)",
        technical_pairs=(("aadhaar", "aadhaar_number"),),
        resolution_rule="FINAL_REPORT_CUSTOMER_KYC_AADHAAR",
    ),
    ReviewedSourcePolicy(
        attribute_key="pan",
        report_field="Pan No",
        business_source_label="Customer KYC (PAN, Aadhaar, address proof)",
        technical_pairs=(("pan_card", "pan_number"),),
        resolution_rule="FINAL_REPORT_CUSTOMER_KYC_PAN",
    ),
    ReviewedSourcePolicy(
        attribute_key="booking_tcs_amount",
        report_field="TCS (Actual)",
        business_source_label="Tax Invoice — DMS",
        technical_pairs=(("customer_invoice_dms", "tcs_amount"),),
        resolution_rule="FINAL_REPORT_TAX_INVOICE_DMS_TCS",
    ),
    ReviewedSourcePolicy(
        attribute_key="dms_invoice_date",
        report_field="DMS Invoice Date",
        business_source_label="Tax Invoice — DMS",
        technical_pairs=(("customer_invoice_dms", "invoice_date"),),
        resolution_rule="FINAL_REPORT_TAX_INVOICE_DMS_DATE",
    ),
    ReviewedSourcePolicy(
        attribute_key="dms_invoice_number",
        report_field="DMS Invoice Number",
        business_source_label="Tax Invoice — DMS",
        technical_pairs=(("customer_invoice_dms", "invoice_number"),),
        resolution_rule="FINAL_REPORT_TAX_INVOICE_DMS_NUMBER",
    ),
    ReviewedSourcePolicy(
        attribute_key="delivery_date",
        report_field="Delivery Date",
        business_source_label="Gate Pass",
        technical_pairs=(("gate_pass", "delivery_date"),),
        resolution_rule="FINAL_REPORT_GATE_PASS_DELIVERY_DATE",
    ),
    ReviewedSourcePolicy(
        attribute_key="gstin",
        report_field="GST",
        business_source_label="GST Certificate",
        technical_pairs=(("gst_certificate", "gstin"),),
        resolution_rule="FINAL_REPORT_GST_CERTIFICATE_GSTIN",
    ),
    ReviewedSourcePolicy(
        attribute_key="new_car_chassis_number",
        report_field="New Car Chasiss No.",
        business_source_label="Tax Invoice — DMS",
        technical_pairs=(("customer_invoice_dms", "chassis_number"),),
        resolution_rule="FINAL_REPORT_TAX_INVOICE_DMS_CHASSIS",
    ),
    ReviewedSourcePolicy(
        attribute_key="bank_name",
        report_field="Bank Name",
        business_source_label="Bank Statement",
        technical_pairs=(("bank_statement", "bank_name"),),
        resolution_rule="FINAL_REPORT_BANK_STATEMENT_BANK_NAME",
    ),
)


# These business source decisions are approved, but the technical Audit Core/DI
# mapping is not yet authoritative. The final-source command must fail closed while
# any of these remain instead of converting presentation labels into guessed keys.
UNRESOLVED_TECHNICAL_POLICIES: tuple[UnresolvedTechnicalPolicy, ...] = (
    UnresolvedTechnicalPolicy(
        "Booking Date",
        "Minimum Booking Amount payment proof",
        "receipt document identity is fragmented across current Audit Core paths",
    ),
    UnresolvedTechnicalPolicy(
        "Pincode",
        "Customer KYC (PAN, Aadhaar, address proof)",
        "authoritative split KYC pincode field mapping is not proven",
    ),
    UnresolvedTechnicalPolicy(
        "KYC District",
        "Customer KYC (PAN, Aadhaar, address proof)",
        "authoritative split KYC district field mapping is not proven",
    ),
    UnresolvedTechnicalPolicy(
        "KYC State",
        "Customer KYC (PAN, Aadhaar, address proof)",
        "authoritative split KYC state field mapping is not proven",
    ),
    UnresolvedTechnicalPolicy(
        "Contact No",
        "Booking Docket (Sales Contract)",
        "published booking_docket vs runtime booking_form identity is unresolved",
    ),
    UnresolvedTechnicalPolicy(
        "Deal Type",
        "Booking Docket (Sales Contract)",
        "authoritative Booking Docket deal-type field key is not proven",
    ),
    UnresolvedTechnicalPolicy(
        "Out of scope reasons",
        "Booking Docket (Sales Contract)",
        "authoritative Booking Docket out-of-scope field key is not proven",
    ),
    UnresolvedTechnicalPolicy(
        "Registration Number / State / Territory / District",
        "RTO Paper",
        "RTO Paper canonical document family and field keys require Step-2 validation",
    ),
    UnresolvedTechnicalPolicy(
        "Finance Type",
        "Bank DO",
        "Bank DO canonical document/field mapping is not proven",
    ),
    UnresolvedTechnicalPolicy(
        "Exchange (Y/N)",
        "Booking Docket (Sales Contract)",
        "published booking_docket vs runtime booking_form identity is unresolved",
    ),
    UnresolvedTechnicalPolicy(
        "DSA Commsission",
        "Booking Docket (Sales Contract)",
        "authoritative DSA commission field key is not proven",
    ),
    UnresolvedTechnicalPolicy(
        "Ex Showroom / Registration Type / HP Charges (Actual)",
        "RTO Paper",
        "RTO Paper commercial field mapping is not proven",
    ),
    UnresolvedTechnicalPolicy(
        "Insurance (Actual)",
        "Insurance Cover Note",
        "catalogue insurance_cover vs resolver insurance_cover_note/policy identity is unresolved",
    ),
    UnresolvedTechnicalPolicy(
        "Accessories (Actual)",
        "Accessory Invoice — Tally / bookkeeping software",
        "authoritative accessory-invoice field mapping is not proven",
    ),
    UnresolvedTechnicalPolicy(
        "EW (Actual)",
        "EW Tally Invoice",
        "authoritative EW invoice document/field mapping is not proven",
    ),
    UnresolvedTechnicalPolicy(
        "RSA / Fast tag / Actual discounts / deal totals",
        "Customer Ledger",
        "Customer Ledger field contract for final report values is not proven",
    ),
)
