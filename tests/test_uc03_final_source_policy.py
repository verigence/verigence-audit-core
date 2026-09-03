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
    assert ("customer_invoice_dms", "tcs_amount") in pairs
    assert ("customer_invoice_dms", "invoice_date") in pairs
    assert ("customer_invoice_dms", "invoice_number") in pairs
    assert ("gate_pass", "delivery_date") in pairs
    assert ("gst_certificate", "gstin") in pairs
    assert ("customer_invoice_dms", "chassis_number") in pairs
    assert ("bank_statement", "bank_name") in pairs

    # Disputed/unverified aliases must not silently enter the executable registry.
    assert not any(document_type == "booking_form" for document_type, _ in pairs)
    assert not any(document_type == "insurance_cover_note" for document_type, _ in pairs)
    assert not any(document_type == "insurance_policy" for document_type, _ in pairs)
    assert not any(document_type == "dealer_receipt" for document_type, _ in pairs)
    assert not any(document_type == "tax_invoice" for document_type, _ in pairs)
    assert not any(document_type == "tax_invoice_dms" for document_type, _ in pairs)


def test_newly_proven_final_report_policies_use_exact_validated_pairs() -> None:
    policies = {policy.report_field: policy for policy in PROVEN_REVIEWED_SOURCE_POLICIES}

    expected = {
        "DMS Invoice Date": (("customer_invoice_dms", "invoice_date"),),
        "DMS Invoice Number": (("customer_invoice_dms", "invoice_number"),),
        "Delivery Date": (("gate_pass", "delivery_date"),),
        "GST": (("gst_certificate", "gstin"),),
        "New Car Chasiss No.": (("customer_invoice_dms", "chassis_number"),),
        "Bank Name": (("bank_statement", "bank_name"),),
    }

    for report_field, technical_pairs in expected.items():
        assert policies[report_field].technical_pairs == technical_pairs


def test_unresolved_registry_keeps_only_still_unproven_canonical_gaps_explicit() -> None:
    rendered = "\n".join(
        f"{item.report_field}|{item.business_source_label}|{item.reason}"
        for item in UNRESOLVED_TECHNICAL_POLICIES
    )
    unresolved_report_fields = {
        item.report_field for item in UNRESOLVED_TECHNICAL_POLICIES
    }

    assert "Booking Docket" in rendered
    assert "RTO Paper" in rendered
    assert "Insurance Cover Note" in rendered
    assert "Customer Ledger" in rendered
    assert "Bank DO" in rendered
    assert "Minimum Booking Amount payment proof" in rendered

    newly_proven = {
        "DMS Invoice Date",
        "DMS Invoice Number",
        "Delivery Date",
        "GST",
        "New Car Chasiss No.",
        "Bank Name",
    }
    assert unresolved_report_fields.isdisjoint(newly_proven)


def test_every_executable_policy_has_business_label_rule_and_exact_pairs() -> None:
    assert PROVEN_REVIEWED_SOURCE_POLICIES
    for policy in PROVEN_REVIEWED_SOURCE_POLICIES:
        assert policy.attribute_key
        assert policy.report_field
        assert policy.business_source_label
        assert policy.resolution_rule.startswith("FINAL_REPORT_")
        assert policy.technical_pairs
        assert all(document_type and field_key for document_type, field_key in policy.technical_pairs)
