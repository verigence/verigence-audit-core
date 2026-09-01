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

    # Disputed/unverified aliases must not silently enter the executable registry.
    assert not any(document_type == "booking_form" for document_type, _ in pairs)
    assert not any(document_type == "insurance_cover_note" for document_type, _ in pairs)
    assert not any(document_type == "insurance_policy" for document_type, _ in pairs)
    assert not any(document_type == "dealer_receipt" for document_type, _ in pairs)
    assert not any(document_type == "tax_invoice" for document_type, _ in pairs)
    assert not any(document_type == "tax_invoice_dms" for document_type, _ in pairs)


def test_unresolved_registry_keeps_known_canonical_gaps_explicit() -> None:
    rendered = "\n".join(
        f"{item.report_field}|{item.business_source_label}|{item.reason}"
        for item in UNRESOLVED_TECHNICAL_POLICIES
    )

    assert "Booking Docket" in rendered
    assert "RTO Paper" in rendered
    assert "Insurance Cover Note" in rendered
    assert "Customer Ledger" in rendered
    assert "Bank Statement" in rendered
    assert "Gate Pass" in rendered
    assert "Tax Invoice — DMS" in rendered


def test_every_executable_policy_has_business_label_rule_and_exact_pairs() -> None:
    assert PROVEN_REVIEWED_SOURCE_POLICIES
    for policy in PROVEN_REVIEWED_SOURCE_POLICIES:
        assert policy.attribute_key
        assert policy.report_field
        assert policy.business_source_label
        assert policy.resolution_rule.startswith("FINAL_REPORT_")
        assert policy.technical_pairs
        assert all(document_type and field_key for document_type, field_key in policy.technical_pairs)
