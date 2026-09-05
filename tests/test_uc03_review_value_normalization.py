from decimal import Decimal

import pytest

from audit_core import uc03_booking_capture
from audit_core.errors import AuditCoreError
from audit_core.uc03_review_value_normalization import (
    install_uc03_review_value_normalization,
    reviewed_decimal,
)


def test_reviewed_decimal_accepts_numeric_scalars_and_indian_currency_text() -> None:
    cases = (
        (500000, Decimal("500000")),
        (500000.25, Decimal("500000.25")),
        ("500000", Decimal("500000")),
        ("5,00,000", Decimal("500000")),
        ("₹ 5,00,000", Decimal("500000")),
        ("INR 5,00,000", Decimal("500000")),
        ("Rs. 5,00,000/-", Decimal("500000")),
        ("5,00,000 INR", Decimal("500000")),
    )

    for value, expected in cases:
        assert reviewed_decimal(value, "TRADE_IN_ACTUAL_VALUE") == expected


@pytest.mark.parametrize("value", [None, True, "", "N/A", "No exchange", "5 lakh"])
def test_reviewed_decimal_rejects_semantically_non_numeric_values(value) -> None:
    with pytest.raises(AuditCoreError) as exc_info:
        reviewed_decimal(value, "TRADE_IN_ACTUAL_VALUE")

    assert exc_info.value.error_code == "VAC-VAL-002"
    assert exc_info.value.detail == "TRADE_IN_ACTUAL_VALUE requires a numeric value."


def test_runtime_installer_updates_shared_uc03_decimal_boundary() -> None:
    install_uc03_review_value_normalization()

    assert uc03_booking_capture._as_decimal("₹ 5,00,000", "TRADE_IN_ACTUAL_VALUE") == Decimal(
        "500000"
    )
