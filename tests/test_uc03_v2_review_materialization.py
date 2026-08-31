from uuid import uuid4

from audit_core.uc03_booking_review_decisions import _raw_review_items
from audit_core.uc03_document_review_v2 import (
    ReviewV2Document,
    ReviewV2Field,
    ReviewV2UnmappedField,
)
from audit_core.uc03_v2_review_materialization import (
    _AADHAAR_FIELDS,
    _BOOKING_FORM_FIELDS,
    _PAN_FIELDS,
    _RECEIPT_FIELDS,
    _reviewed_receipt_values,
    receipt_document_ordinals,
    receipt_review_key,
)


def _field(field_key: str, value, *, confidence: float = 99.0) -> ReviewV2Field:
    return ReviewV2Field(
        canonicalFieldId=str(uuid4()),
        fieldKey=field_key,
        value=value,
        confidenceScore=confidence,
        sourceFactVersion=1,
        reviewState="READY" if confidence >= 92.0 else "NEEDS_REVIEW",
    )


def _receipt(document_id, *, amount: str, confidence: float = 99.0) -> ReviewV2Document:
    return ReviewV2Document(
        documentId=document_id,
        label="Dealer Receipt",
        documentTypeKey="dealer_receipt",
        originalFilename=f"{document_id}.pdf",
        processingStatus="PROCESSED",
        extractionState="READY",
        fields=[
            _field("receipt_number", f"R-{str(document_id)[:6]}"),
            _field("receipt_date", "2026-08-30"),
            _field("amount_paid", amount, confidence=confidence),
            _field("payment_mode", "UPI"),
            _field("payment_reference_no", "UTR-123"),
        ],
    )


def _receipt_unmapped(document_id, *, amount: str, confidence: float):
    return ReviewV2UnmappedField(
        canonicalFieldId=str(uuid4()),
        fieldKey="amount_paid",
        value=amount,
        confidenceScore=confidence,
        sourceFactVersion=1,
        documentId=document_id,
        documentTypeKey="dealer_receipt",
        documentLabel="Dealer Receipt",
        originalFilename=f"{document_id}.pdf",
    )


def test_booking_form_di_contract_has_core_review_owner_for_every_field() -> None:
    # Mirrors verigence-di booking_form schema v1.5 exactly. This test must change
    # whenever DI adds/removes a Booking Form extraction field.
    assert set(_BOOKING_FORM_FIELDS) == {
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
        "extended_warranty_amount",
        "accessories_cost",
        "essential_kit_amount",
        "genuine_accessories_amount",
        "non_genuine_accessories_amount",
        "fastag_amount",
        "green_tax_amount",
        "service_package_amount",
        "other_charges",
        "discount_amount",
        "sales_discount_amount",
        "buffer_discount_amount",
        "exchange_discount_amount",
        "corporate_discount_amount",
        "loyalty_discount_amount",
        "inhouse_insurance_discount_amount",
        "mr_discount_amount",
        "oem_referral_discount_amount",
        "other_discount_amount",
        "free_accessory_discount_amount",
        "bonus_amount",
        "total_price",
        "net_amount",
        "booking_amount_paid",
        "balance_amount",
        "mode_of_payment",
        "payment_reference_no",
        "expected_delivery",
        "expected_delivery_date",
    }


def test_pan_di_contract_has_core_review_owner_for_every_field() -> None:
    assert _PAN_FIELDS == {
        "pan_number",
        "pan_name",
        "pan_father_name",
        "pan_relationship_type",
        "pan_relationship_name",
        "date_of_birth",
    }


def test_aadhaar_di_contract_has_core_review_owner_for_every_field() -> None:
    assert _AADHAAR_FIELDS == {
        "aadhaar_number",
        "aadhaar_name",
        "date_of_birth",
        "gender",
        "aadhaar_address",
        "aadhaar_relationship_type",
        "aadhaar_relationship_name",
    }


def test_dealer_receipt_di_contract_has_core_review_owner_for_every_field() -> None:
    assert set(_RECEIPT_FIELDS) == {
        "dealer_name",
        "dealer_gstin",
        "customer_name",
        "customer_phone",
        "receipt_number",
        "receipt_date",
        "amount_paid",
        "payment_mode",
        "payment_reference_no",
        "payment_reference_date",
        "bank_name",
        "bank_location",
        "booking_reference_number",
        "remarks",
        "amount_in_words",
    }


def test_receipt_review_key_is_receipt_scoped() -> None:
    assert receipt_review_key(1, "amount_paid") != receipt_review_key(
        2, "amount_paid"
    )


def test_receipt_ordinals_are_deterministic() -> None:
    first = uuid4()
    second = uuid4()
    forward = receipt_document_ordinals([first, second])
    reverse = receipt_document_ordinals([second, first])

    assert forward == reverse
    assert set(forward.values()) == {1, 2}


def test_two_receipts_with_different_amounts_are_not_cross_source_mismatch() -> None:
    first = uuid4()
    second = uuid4()
    ordinals = receipt_document_ordinals([first, second])

    items = _raw_review_items(
        [
            _receipt_unmapped(first, amount="20000", confidence=99.0),
            _receipt_unmapped(second, amount="30000", confidence=99.0),
        ]
    )

    assert len(items) == 2
    assert {item.review_key for item in items} == {
        receipt_review_key(ordinals[first], "amount_paid"),
        receipt_review_key(ordinals[second], "amount_paid"),
    }
    assert all(item.decision_required is False for item in items)


def test_low_confidence_receipt_field_requires_only_its_own_decision() -> None:
    first = uuid4()
    second = uuid4()
    ordinals = receipt_document_ordinals([first, second])

    items = {
        item.review_key: item
        for item in _raw_review_items(
            [
                _receipt_unmapped(first, amount="20000", confidence=80.0),
                _receipt_unmapped(second, amount="30000", confidence=99.0),
            ]
        )
    }

    assert items[
        receipt_review_key(ordinals[first], "amount_paid")
    ].decision_required is True
    assert items[
        receipt_review_key(ordinals[second], "amount_paid")
    ].decision_required is False


def test_rejected_receipt_amount_is_not_materialized_as_zero_payment() -> None:
    document_id = uuid4()
    document = _receipt(document_id, amount="50000")

    values = _reviewed_receipt_values(
        document,
        receipt_ordinal=1,
        rejected_review_keys={receipt_review_key(1, "amount_paid")},
    )

    assert "amount_paid" not in values
    assert values["receipt_number"] is not None


def test_reviewed_receipt_fields_are_collected_once_in_memory() -> None:
    document_id = uuid4()
    document = _receipt(document_id, amount="50000")

    values = _reviewed_receipt_values(
        document,
        receipt_ordinal=1,
        rejected_review_keys=set(),
    )

    assert str(values["amount_paid"]) == "50000"
    assert str(values["receipt_date"]) == "2026-08-30"
    assert values["payment_mode"] == "UPI"
    assert values["payment_reference_no"] == "UTR-123"
