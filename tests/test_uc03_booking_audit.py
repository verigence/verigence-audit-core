from datetime import date
from decimal import Decimal
from uuid import UUID

from audit_core.uc03_booking_audit import (
    ProductIdentity,
    Receipt,
    derive_booking_confirmation_date,
    names_logically_equal,
)


def _project_policy(*, amount: str, effective_from: date = date(2026, 1, 1)):
    version_id = UUID("10000000-0000-0000-0000-000000000001")
    return [
        {
            "discount_policy_version_id": version_id,
            "effective_from": effective_from,
            "effective_to": None,
            "parameter_id": UUID("20000000-0000-0000-0000-000000000001"),
            "scope_type": "PROJECT",
            "segment_id": None,
            "scope_key": None,
            "value_number": Decimal(amount),
        }
    ]


def test_confirmation_date_is_first_receipt_that_crosses_cumulative_minimum():
    receipts = [
        Receipt(UUID("30000000-0000-0000-0000-000000000001"), Decimal("20000"), date(2026, 8, 5)),
        Receipt(UUID("30000000-0000-0000-0000-000000000002"), Decimal("15000"), date(2026, 8, 9)),
        Receipt(UUID("30000000-0000-0000-0000-000000000003"), Decimal("20000"), date(2026, 8, 12)),
    ]

    confirmed_on, minimum, cumulative, issue = derive_booking_confirmation_date(
        receipts,
        _project_policy(amount="50000"),
        identity=None,
    )

    assert confirmed_on == date(2026, 8, 12)
    assert minimum == Decimal("50000")
    assert cumulative == Decimal("55000")
    assert issue is None


def test_confirmation_date_does_not_use_source_booking_date_or_first_receipt_date():
    receipts = [
        Receipt(UUID("30000000-0000-0000-0000-000000000011"), Decimal("49000"), date(2026, 8, 1)),
        Receipt(UUID("30000000-0000-0000-0000-000000000012"), Decimal("1000"), date(2026, 8, 20)),
    ]

    confirmed_on, _, _, issue = derive_booking_confirmation_date(
        receipts,
        _project_policy(amount="50000"),
        identity=None,
    )

    assert confirmed_on == date(2026, 8, 20)
    assert issue is None


def test_verified_receipt_without_date_prevents_confirmation_date_derivation():
    receipts = [
        Receipt(UUID("30000000-0000-0000-0000-000000000021"), Decimal("50000"), None),
    ]

    confirmed_on, _, _, issue = derive_booking_confirmation_date(
        receipts,
        _project_policy(amount="50000"),
        identity=None,
    )

    assert confirmed_on is None
    assert issue is not None
    assert issue.rule_key == "BK_PAYMENT_RECEIPT_DATE_MISSING"


def test_more_specific_minimum_booking_master_scope_wins_without_sku_dependency():
    version_id = UUID("10000000-0000-0000-0000-000000000021")
    segment_id = UUID("40000000-0000-0000-0000-000000000001")
    identity = ProductIdentity(
        model_id=UUID("50000000-0000-0000-0000-000000000001"),
        variant_id=UUID("60000000-0000-0000-0000-000000000001"),
        segment_id=segment_id,
        model_code="XUV700",
        model_name="XUV700",
        variant_code="AX7",
        variant_name="AX7",
    )
    policy_rows = [
        {
            "discount_policy_version_id": version_id,
            "effective_from": date(2026, 1, 1),
            "effective_to": None,
            "parameter_id": UUID("70000000-0000-0000-0000-000000000001"),
            "scope_type": "PROJECT",
            "segment_id": None,
            "scope_key": None,
            "value_number": Decimal("50000"),
        },
        {
            "discount_policy_version_id": version_id,
            "effective_from": date(2026, 1, 1),
            "effective_to": None,
            "parameter_id": UUID("70000000-0000-0000-0000-000000000002"),
            "scope_type": "MODEL",
            "segment_id": segment_id,
            "scope_key": "XUV700",
            "value_number": Decimal("75000"),
        },
    ]
    receipts = [
        Receipt(UUID("30000000-0000-0000-0000-000000000031"), Decimal("50000"), date(2026, 8, 1)),
        Receipt(UUID("30000000-0000-0000-0000-000000000032"), Decimal("25000"), date(2026, 8, 2)),
    ]

    confirmed_on, minimum, cumulative, issue = derive_booking_confirmation_date(
        receipts,
        policy_rows,
        identity=identity,
    )

    assert confirmed_on == date(2026, 8, 2)
    assert minimum == Decimal("75000")
    assert cumulative == Decimal("75000")
    assert issue is None


def test_name_comparison_is_deterministic_and_handles_initials_without_fuzzy_matching():
    assert names_logically_equal("Rahul Kumar Sharma", "Rahul K Sharma")
    assert names_logically_equal("Mr Rahul Sharma", "RAHUL SHARMA")
    assert names_logically_equal("Rahul Sharma S/O Mohan Sharma", "Rahul Sharma")
    assert not names_logically_equal("Rahul Sharma", "Rakesh Sharma")
