from audit_core.uc03_booking_part1 import _normalized
from audit_core.uc03_booking_receipt_capture import _RECEIPT_CAPTURE_MAP


def test_part1_master_normalization_is_exact_but_format_tolerant() -> None:
    assert _normalized("Scorpio N") == "scorpion"
    assert _normalized("  Z8-L  ") == "z8l"
    assert _normalized(None) is None


def test_dealer_receipt_fields_use_generic_pc_review_contract() -> None:
    assert set(_RECEIPT_CAPTURE_MAP) == {
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
    assert _RECEIPT_CAPTURE_MAP["amount_paid"] == "RECEIPT_AMOUNT"
    assert _RECEIPT_CAPTURE_MAP["customer_phone"] == "RECEIPT_CUSTOMER_PHONE"
    assert (
        _RECEIPT_CAPTURE_MAP["booking_reference_number"]
        == "RECEIPT_BOOKING_REFERENCE"
    )
