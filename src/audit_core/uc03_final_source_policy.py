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
        attribute_key="pincode",
        report_field="Pincode",
        business_source_label="Customer KYC (PAN, Aadhaar, address proof)",
        technical_pairs=(("aadhaar", "address_pincode"),),
        resolution_rule="FINAL_REPORT_CUSTOMER_KYC_PINCODE",
    ),
    ReviewedSourcePolicy(
        attribute_key="kyc_district",
        report_field="KYC District",
        business_source_label="Customer KYC (PAN, Aadhaar, address proof)",
        technical_pairs=(("aadhaar", "address_district"),),
        resolution_rule="FINAL_REPORT_CUSTOMER_KYC_DISTRICT",
    ),
    ReviewedSourcePolicy(
        attribute_key="kyc_state",
        report_field="KYC State",
        business_source_label="Customer KYC (PAN, Aadhaar, address proof)",
        technical_pairs=(("aadhaar", "address_state"),),
        resolution_rule="FINAL_REPORT_CUSTOMER_KYC_STATE",
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
    ReviewedSourcePolicy(
        attribute_key="contact_number",
        report_field="Contact No",
        business_source_label="Booking Docket (Sales Contract)",
        technical_pairs=(("booking_docket", "customer_phone"),),
        resolution_rule="FINAL_REPORT_BOOKING_DOCKET_CONTACT_NO",
    ),
    ReviewedSourcePolicy(
        attribute_key="deal_type",
        report_field="Deal Type",
        business_source_label="Booking Docket (Sales Contract)",
        technical_pairs=(("booking_docket", "deal_type"),),
        resolution_rule="FINAL_REPORT_BOOKING_DOCKET_DEAL_TYPE",
    ),
    ReviewedSourcePolicy(
        attribute_key="out_of_scope_reasons",
        report_field="Out of scope reasons",
        business_source_label="Booking Docket (Sales Contract)",
        technical_pairs=(("booking_docket", "out_of_scope_reasons"),),
        resolution_rule="FINAL_REPORT_BOOKING_DOCKET_OUT_OF_SCOPE_REASONS",
    ),
    ReviewedSourcePolicy(
        attribute_key="exchange_applicable",
        report_field="Exchange (Y/N)",
        business_source_label="Booking Docket (Sales Contract)",
        technical_pairs=(("booking_docket", "exchange_applicable"),),
        resolution_rule="FINAL_REPORT_BOOKING_DOCKET_EXCHANGE_APPLICABLE",
    ),
    ReviewedSourcePolicy(
        attribute_key="dsa_commission_amount",
        report_field="DSA Commsission",
        business_source_label="Booking Docket (Sales Contract)",
        technical_pairs=(("booking_docket", "dsa_commission_amount"),),
        resolution_rule="FINAL_REPORT_BOOKING_DOCKET_DSA_COMMISSION",
    ),
    ReviewedSourcePolicy(
        attribute_key="registration_number",
        report_field="Registration Number",
        business_source_label="RTO Paper",
        technical_pairs=(("rto_challan", "registration_number"),),
        resolution_rule="FINAL_REPORT_RTO_REGISTRATION_NUMBER",
    ),
    ReviewedSourcePolicy(
        attribute_key="registration_state",
        report_field="Registration State",
        business_source_label="RTO Paper",
        technical_pairs=(("rto_challan", "registration_state"),),
        resolution_rule="FINAL_REPORT_RTO_REGISTRATION_STATE",
    ),
    ReviewedSourcePolicy(
        attribute_key="registration_territory",
        report_field="Territory Categorization",
        business_source_label="RTO Paper",
        technical_pairs=(("rto_challan", "registration_territory"),),
        resolution_rule="FINAL_REPORT_RTO_REGISTRATION_TERRITORY",
    ),
    ReviewedSourcePolicy(
        attribute_key="registration_district",
        report_field="Registration District",
        business_source_label="RTO Paper",
        technical_pairs=(("rto_challan", "registration_district"),),
        resolution_rule="FINAL_REPORT_RTO_REGISTRATION_DISTRICT",
    ),
    ReviewedSourcePolicy(
        attribute_key="ex_showroom_amount",
        report_field="Ex Showroom (Actual)",
        business_source_label="RTO Paper",
        technical_pairs=(("rto_challan", "ex_showroom_amount"),),
        resolution_rule="FINAL_REPORT_RTO_EX_SHOWROOM_ACTUAL",
    ),
    ReviewedSourcePolicy(
        attribute_key="registration_type",
        report_field="Registration Type",
        business_source_label="RTO Paper",
        technical_pairs=(("rto_challan", "registration_type"),),
        resolution_rule="FINAL_REPORT_RTO_REGISTRATION_TYPE",
    ),
    ReviewedSourcePolicy(
        attribute_key="hp_charges_amount",
        report_field="HP Charges (Actual)",
        business_source_label="RTO Paper",
        technical_pairs=(("rto_challan", "hp_charges_amount"),),
        resolution_rule="FINAL_REPORT_RTO_HP_CHARGES_ACTUAL",
    ),
    ReviewedSourcePolicy(
        attribute_key="insurance_actual_amount",
        report_field="Insurance (Actual)",
        business_source_label="Insurance Cover Note",
        technical_pairs=(("insurance_cover", "premium_amount"),),
        resolution_rule="FINAL_REPORT_INSURANCE_COVER_PREMIUM_ACTUAL",
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
        "Finance Type",
        "Bank DO",
        "Bank DO canonical document/field mapping is not proven",
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
