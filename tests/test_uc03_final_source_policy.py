from audit_core.uc03_final_source_policy import (
    PROVEN_REVIEWED_SOURCE_POLICIES,
    UNRESOLVED_TECHNICAL_POLICIES,
)


def test_proven_policy_registry_contains_only_exact_audit_core_contract_keys() -> None:
    pairs = {
        pair
        for policy in PROVEN_REVIEWED_SOURCE_POLICIES
        for pair in policy.technical_pairs
    }

    assert ("pan_card", "pan_name") in pairs
    assert ("aadhaar", "aadhaar_name") in pairs
    assert ("aadhaar", "aadhaar_number") in pairs
    assert ("pan_card", "pan_number") in pairs
    assert ("aadhaar", "address_pincode") in pairs
    assert ("aadhaar", "address_district") in pairs
    assert ("aadhaar", "address_state") in pairs
    assert ("customer_invoice_dms", "tcs_amount") in pairs
    assert ("customer_invoice_dms", "invoice_date") in pairs
    assert ("customer_invoice_dms", "invoice_number") in pairs
    assert ("gate_pass", "delivery_date") in pairs
    assert ("gst_certificate", "gstin") in pairs
    assert ("customer_invoice_dms", "chassis_number") in pairs
    assert ("bank_statement", "bank_name") in pairs
    assert ("booking_docket", "customer_phone") in pairs
    assert ("booking_docket", "deal_type") in pairs
    assert ("booking_docket", "out_of_scope_reasons") in pairs
    assert ("booking_docket", "exchange_applicable") in pairs
    assert ("booking_docket", "dsa_commission_amount") in pairs
    assert ("rto_challan", "registration_number") in pairs
    assert ("rto_challan", "registration_state") in pairs
    assert ("rto_challan", "registration_territory") in pairs
    assert ("rto_challan", "registration_district") in pairs
    assert ("rto_challan", "ex_showroom_amount") in pairs
    assert ("rto_challan", "registration_type") in pairs
    assert ("rto_challan", "hp_charges_amount") in pairs
    assert ("insurance_cover", "premium_amount") in pairs

    # Disputed/unverified aliases must not silently enter the executable registry.
    assert not any(document_type == "booking_form" for document_type, _ in pairs)
    assert not any(document_type == "insurance_cover_note" for document_type, _ in pairs)
    assert not any(document_type == "insurance_policy" for document_type, _ in pairs)
    assert not any(document_type == "dealer_receipt" for document_type, _ in pairs)
    assert not any(document_type == "tax_invoice" for document_type, _ in pairs)
    assert not any(document_type == "tax_invoice_dms" for document_type, _ in pairs)
    assert not any(document_type == "vehicle_rc" for document_type, _ in pairs)


def test_newly_proven_final_report_policies_use_exact_validated_pairs() -> None:
    policies = {policy.report_field: policy for policy in PROVEN_REVIEWED_SOURCE_POLICIES}

    expected = {
        "Pincode": (("aadhaar", "address_pincode"),),
        "KYC District": (("aadhaar", "address_district"),),
        "KYC State": (("aadhaar", "address_state"),),
        "DMS Invoice Date": (("customer_invoice_dms", "invoice_date"),),
        "DMS Invoice Number": (("customer_invoice_dms", "invoice_number"),),
        "Delivery Date": (("gate_pass", "delivery_date"),),
        "GST": (("gst_certificate", "gstin"),),
        "New Car Chasiss No.": (("customer_invoice_dms", "chassis_number"),),
        "Bank Name": (("bank_statement", "bank_name"),),
        "Contact No": (("booking_docket", "customer_phone"),),
        "Deal Type": (("booking_docket", "deal_type"),),
        "Out of scope reasons": (("booking_docket", "out_of_scope_reasons"),),
        "Exchange (Y/N)": (("booking_docket", "exchange_applicable"),),
        "DSA Commsission": (("booking_docket", "dsa_commission_amount"),),
        "Registration Number": (("rto_challan", "registration_number"),),
        "Registration State": (("rto_challan", "registration_state"),),
        "Territory Categorization": (("rto_challan", "registration_territory"),),
        "Registration District": (("rto_challan", "registration_district"),),
        "Ex Showroom (Actual)": (("rto_challan", "ex_showroom_amount"),),
        "Registration Type": (("rto_challan", "registration_type"),),
        "HP Charges (Actual)": (("rto_challan", "hp_charges_amount"),),
        "Insurance (Actual)": (("insurance_cover", "premium_amount"),),
    }

    for report_field, technical_pairs in expected.items():
        assert policies[report_field].technical_pairs == technical_pairs


def test_rto_report_fields_are_independent_scalar_policies() -> None:
    rto_policies = [
        policy
        for policy in PROVEN_REVIEWED_SOURCE_POLICIES
        if policy.business_source_label == "RTO Paper"
    ]

    assert len(rto_policies) == 7
    assert {policy.attribute_key for policy in rto_policies} == {
        "registration_number",
        "registration_state",
        "registration_territory",
        "registration_district",
        "ex_showroom_amount",
        "registration_type",
        "hp_charges_amount",
    }
    assert all(len(policy.technical_pairs) == 1 for policy in rto_policies)
    assert all(policy.technical_pairs[0][0] == "rto_challan" for policy in rto_policies)


def test_kyc_address_policies_are_explicit_aadhaar_values_without_aliases() -> None:
    policies = {policy.report_field: policy for policy in PROVEN_REVIEWED_SOURCE_POLICIES}

    assert policies["Pincode"].attribute_key == "pincode"
    assert policies["KYC District"].attribute_key == "kyc_district"
    assert policies["KYC State"].attribute_key == "kyc_state"
    assert policies["Pincode"].technical_pairs == (("aadhaar", "address_pincode"),)
    assert policies["KYC District"].technical_pairs == (("aadhaar", "address_district"),)
    assert policies["KYC State"].technical_pairs == (("aadhaar", "address_state"),)


def test_insurance_actual_uses_published_insurance_cover_premium_only() -> None:
    policies = {policy.report_field: policy for policy in PROVEN_REVIEWED_SOURCE_POLICIES}
    insurance = policies["Insurance (Actual)"]

    assert insurance.attribute_key == "insurance_actual_amount"
    assert insurance.business_source_label == "Insurance Cover Note"
    assert insurance.technical_pairs == (("insurance_cover", "premium_amount"),)


def test_booking_date_is_typed_owner_not_document_resolution_policy() -> None:
    executable_fields = {
        policy.report_field for policy in PROVEN_REVIEWED_SOURCE_POLICIES
    }
    unresolved_fields = {
        policy.report_field for policy in UNRESOLVED_TECHNICAL_POLICIES
    }

    assert "Booking Date" not in executable_fields
    assert "Booking Date" not in unresolved_fields


def test_first_receipt_date_remains_fail_closed_on_money_receipt_identity() -> None:
    unresolved = {
        policy.report_field: policy for policy in UNRESOLVED_TECHNICAL_POLICIES
    }

    first_receipt = unresolved["First receipt date"]
    assert first_receipt.business_source_label == "Money Receipt"
    assert "document identity" in first_receipt.reason
    assert "receipt-date field mapping" in first_receipt.reason


def test_unresolved_registry_keeps_only_still_unproven_canonical_gaps_explicit() -> None:
    rendered = "\n".join(
        f"{item.report_field}|{item.business_source_label}|{item.reason}"
        for item in UNRESOLVED_TECHNICAL_POLICIES
    )
    unresolved_report_fields = {
        item.report_field for item in UNRESOLVED_TECHNICAL_POLICIES
    }

    assert "RTO Paper" not in rendered
    assert "Insurance Cover Note" not in rendered
    assert "Minimum Booking Amount payment proof" not in rendered
    assert "Money Receipt" in rendered
    assert "Customer Ledger" in rendered
    assert "Bank DO" in rendered
    assert "aggregation semantics" in rendered
    assert "Tally-specific EW invoice identity" in rendered

    newly_proven = {
        "Pincode",
        "KYC District",
        "KYC State",
        "DMS Invoice Date",
        "DMS Invoice Number",
        "Delivery Date",
        "GST",
        "New Car Chasiss No.",
        "Bank Name",
        "Contact No",
        "Deal Type",
        "Out of scope reasons",
        "Exchange (Y/N)",
        "DSA Commsission",
        "Registration Number",
        "Registration State",
        "Territory Categorization",
        "Registration District",
        "Ex Showroom (Actual)",
        "Registration Type",
        "HP Charges (Actual)",
        "Insurance (Actual)",
        "Booking Date",
    }
    assert unresolved_report_fields.isdisjoint(newly_proven)
    assert "First receipt date" in unresolved_report_fields


def test_every_executable_policy_has_business_label_rule_and_exact_pairs() -> None:
    assert PROVEN_REVIEWED_SOURCE_POLICIES
    for policy in PROVEN_REVIEWED_SOURCE_POLICIES:
        assert policy.attribute_key
        assert policy.report_field
        assert policy.business_source_label
        assert policy.resolution_rule.startswith("FINAL_REPORT_")
        assert policy.technical_pairs
        assert all(document_type and field_key for document_type, field_key in policy.technical_pairs)
