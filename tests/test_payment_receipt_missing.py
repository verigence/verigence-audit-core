"""PAYMENT_RECEIPT_MISSING -- the Audit Summary's note on a shortfall waiting for
receipts, and the check's registration. The check itself (the payment receipts
against the NDC as sums, product owner 2026-11-17) is tested on real Postgres in
tests/test_ndc_three_sums.py; the line-by-line matching it replaced is gone."""
from __future__ import annotations

from decimal import Decimal

from audit_core import uc03_p2_audit_rules as rules


def test_the_audit_summary_says_what_a_shortfall_is_waiting_for():
    from audit_core.uc03_p2_audit_summary import settlement

    short = settlement(Decimal(1509000), Decimal(1432000), receipts_missing=Decimal(77000))
    assert short["status"] == "SHORT" and short["receiptsMissing"] == "77000.00"
    assert "no payment receipt on file" in short["reason"] and "77,000" in short["reason"]
    assert settlement(Decimal(1509000), Decimal(1432000))["reason"] == "Received less than the net payable."
    assert settlement(Decimal(1509000), Decimal(1509000), receipts_missing=None)["reason"] is None


class _Facts:
    def __init__(self, short: Decimal) -> None:
        self.missing_receipts_total = short


def test_a_shortfall_no_larger_than_the_receipts_missing_waits_for_them():
    """One cause, one task: the short-payment checks wait while the shortfall is the
    receipts the missing-receipt task asks for."""
    waiting = rules._awaiting_receipts(_Facts(Decimal(21000)), Decimal(21000), Decimal(1))
    assert waiting is not None and "21,000" in waiting and "Payment receipt missing" in waiting
    assert rules._awaiting_receipts(_Facts(Decimal(21000)), Decimal(50000), Decimal(1)) is None
    assert rules._awaiting_receipts(_Facts(Decimal(21000)), Decimal(0), Decimal(1)) is None
    assert rules._awaiting_receipts(_Facts(Decimal(0)), Decimal(21000), Decimal(1)) is None


def test_the_rule_is_registered_as_a_pc_task():
    from audit_core.uc03_p2_document_task_groups import document_category
    from audit_core.uc03_p2_registry import get_registry

    assert "PAYMENT_RECEIPT_MISSING" in rules.RULE_CODES["BOOKING"]
    control = get_registry().controls["PAYMENT_RECEIPT_MISSING"]
    assert control.executor == "NATIVE" and control.mode == "RERUN"
    assert document_category("PC_CONFIRMATION", "PROCESS_CONFIRMATION",
                             "PAYMENT_RECEIPT_MISSING") == "MISSING_DOCUMENT"
