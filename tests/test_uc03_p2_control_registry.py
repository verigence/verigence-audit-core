from __future__ import annotations

from audit_core.uc03_p2_control_registry import (
    P2_CONTROL_REGISTRY,
    P2_EXTERNAL_CONTROLS,
    P2_NATIVE_CONTROLS,
    control_codes_for_stage,
)


def test_p2_control_registry_matches_verified_enabled_baseline() -> None:
    assert len(P2_NATIVE_CONTROLS) == 25
    assert len(P2_EXTERNAL_CONTROLS) == 47
    assert len(P2_CONTROL_REGISTRY) == 72
    assert set(P2_NATIVE_CONTROLS).isdisjoint(P2_EXTERNAL_CONTROLS)


def test_control_registry_preserves_known_split_receipt_rules() -> None:
    assert "BOOKING_RECEIPT_SUM_VS_BOOKING_FORM" in P2_EXTERNAL_CONTROLS
    assert "DELIVERY_RECEIPT_SUM_VS_INVOICE" in P2_EXTERNAL_CONTROLS
    assert "PAYMENT_SUM_VS_INVOICE" not in P2_EXTERNAL_CONTROLS


def test_control_registry_excludes_parked_external_rules() -> None:
    parked = {
        "BOOKING_DOCKET_MISSING",
        "DISCOUNT_APPROVAL_MISSING",
        "LOAN_VS_LEDGER_CREDIT",
        "EXCHANGE_VALUE_BELOW_MARKET",
        "DUPLICATE_PAN_ACROSS_BOOKINGS",
        "DUPLICATE_AADHAAR_ACROSS_BOOKINGS",
        "DUPLICATE_CHASSIS_ACROSS_INVOICES",
        "DUPLICATE_CHASSIS_ACROSS_GATE_PASSES",
        "DUPLICATE_RECEIPT_ACROSS_CASES",
        "DUPLICATE_UTR_ACROSS_CASES",
    }
    assert parked.isdisjoint(P2_EXTERNAL_CONTROLS)


def test_stage_control_views_are_not_empty_and_are_distinct() -> None:
    booking = control_codes_for_stage("BOOKING")
    delivery = control_codes_for_stage("DELIVERY")
    assert booking
    assert delivery
    assert "BK_DOCKET_PRESENT" in booking
    assert "GATE_PASS_MISSING" in delivery
    assert "GATE_PASS_MISSING" not in booking
