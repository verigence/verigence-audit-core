from __future__ import annotations

import pytest

from audit_core.uc03_payment_mode import (
    PAYMENT_MODE_CODES,
    PAYMENT_MODE_TYPES,
    classify_payment_mode,
)


def test_payment_mode_types_codes_match_the_constant_set() -> None:
    assert PAYMENT_MODE_CODES == {code for code, _ in PAYMENT_MODE_TYPES}
    assert "OTHERS" in PAYMENT_MODE_CODES


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Cash", "CASH"),
        ("CASH DEPOSIT", "CASH"),
        ("Cheque", "CHEQUE"),
        ("CHQ", "CHEQUE"),
        ("DD", "CHEQUE"),
        ("Demand Draft", "CHEQUE"),
        ("NEFT", "NEFT"),
        ("NEFT Transfer", "NEFT"),
        ("RTGS", "RTGS"),
        ("IMPS", "IMPS"),
        ("IMPS/P2A/1234567/ABC MOTORS", "IMPS"),
        ("Bank Transfer", "BANK_TRANSFER"),
        ("Net Banking", "BANK_TRANSFER"),
        ("Fund Transfer", "BANK_TRANSFER"),
        ("BO", "BANKERS_ORDER"),
        ("Banker's Order", "BANKERS_ORDER"),
        ("Bankers Order", "BANKERS_ORDER"),
        ("PO", "PAY_ORDER"),
        ("Pay Order", "PAY_ORDER"),
        ("PayOrder", "PAY_ORDER"),
        ("Trade-In", "TRADE_IN"),
        ("Trade In", "TRADE_IN"),
        ("Refund", "REFUND"),
        ("UPI", "OTHERS"),
        ("Card", "OTHERS"),
        ("", "OTHERS"),
        (None, "OTHERS"),
    ],
)
def test_classify_payment_mode_recognizes_expected_values(raw, expected) -> None:
    assert classify_payment_mode(raw) == expected


def test_classify_payment_mode_does_not_false_positive_on_short_tokens_fused_into_a_reference() -> None:
    # "PO"/"DD"/"BO" are only recognized as a standalone word -- not as a
    # substring inside an unrelated alphanumeric reference number.
    assert classify_payment_mode("PO1234567") == "OTHERS"
    assert classify_payment_mode("DD98765") == "OTHERS"


def test_classify_payment_mode_checks_multiple_values_in_order_and_returns_first_match() -> None:
    # Mirrors a bank statement line: transaction_description then reference_no.
    assert classify_payment_mode("Salary credit", "NEFT-UTR12345") == "NEFT"
    assert classify_payment_mode(None, "") == "OTHERS"


def test_classify_payment_mode_bank_narration_examples() -> None:
    assert classify_payment_mode("NEFT-CMS12345-ABC MOTORS PVT LTD") == "NEFT"
    assert classify_payment_mode("RTGS/HDFC0001234/XYZ AUTOS") == "RTGS"
    assert classify_payment_mode("BY CASH DEPOSIT AT BRANCH") == "CASH"
    assert classify_payment_mode("CHQ DEP 004521") == "CHEQUE"
