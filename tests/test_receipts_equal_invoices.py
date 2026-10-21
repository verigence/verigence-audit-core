"""RECEIPTS_EQUAL_INVOICES -- all receipts together equal all invoices
together, within Rs 1, once the settlement window after delivery has closed
(decision 2026-10-19, product owner).

A bank loan is paid to the dealer against a dealer receipt, so it is already a
receipt; an exchange is on the invoice as a discount or line, so it is already
in the invoice total. Credit notes reduce the bill; the dealer's wholesale
invoice is not the customer's bill; DMS and Tally invoices of one sale are one
bill; the same invoice uploaded twice counts once.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_audit_rules as rules
from audit_core.db import set_tenant_context


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="rcpinv")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _delivered(journey, days_ago: int) -> None:
    day = (datetime.now(UTC).date() - timedelta(days=days_ago)).isoformat()
    add_ready_document(journey, "gate_pass", delivery_date=day)


def _check(journey) -> rules.RuleOutcome:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return rules.receipts_equal_invoices(rules._Facts(connection, journey.tenant_id, journey.journey_id))


def _bill(journey) -> None:
    """Rs 5,15,000 billed: vehicle 5,00,000 (the DMS and Tally copies of one
    sale: different numbers, one vehicle), accessories 20,000 (uploaded
    twice), less a 5,000 credit note. The dealer's own wholesale invoice is
    on file too and is not the customer's."""
    add_ready_document(journey, "customer_invoice_dms", invoice_number="DMS-1", vin_number="MA1VIN0001",
                       grand_total_amount="500000")
    add_ready_document(journey, "tax_invoice_tally", invoice_number="TL-1", vin_number="MA1VIN0001",
                       grand_total_amount="500000")
    add_ready_document(journey, "accessory_invoice_dms", invoice_number="ACC-1", grand_total_amount="20000")
    add_ready_document(journey, "accessory_invoice_dms", invoice_number="ACC-1", grand_total_amount="20000")
    add_ready_document(journey, "credit_note", invoice_number="CN-1", grand_total_amount="5000")
    add_ready_document(journey, "wholesale_invoice", invoice_number="WS-1", grand_total_amount="450000")


def test_receipts_matching_all_invoices_within_one_rupee_pass(journey):
    _bill(journey)
    _delivered(journey, days_ago=30)
    add_receipt_payment(journey, amount="11000", receipt_number="R-1", receipt_date="2026-09-01")
    # the bank loan, against a dealer receipt
    add_receipt_payment(journey, amount="400000", receipt_number="R-2", receipt_date="2026-09-20")
    add_receipt_payment(journey, amount="103999.50", receipt_number="R-3", receipt_date="2026-09-21")
    result = _check(journey)
    assert result.outcome == "PASS", result.reason
    assert Decimal(result.details["invoiced"]) == Decimal(515000)
    assert Decimal(result.details["difference"]) == Decimal("-0.50")
    # One copy stands for the vehicle invoice (the canonical type priority:
    # Tally, then DMS); the other is listed as its copy.
    assert {i["documentType"] for i in result.details["invoices"]} == {
        "tax_invoice_tally", "accessory_invoice_dms", "credit_note",
    }
    vehicle = next(i for i in result.details["invoices"] if i["vehicle"])
    assert len(vehicle["copies"]) == 1


def test_receipts_short_of_the_invoices_fail_with_the_gap(journey):
    _bill(journey)
    _delivered(journey, days_ago=30)
    add_receipt_payment(journey, amount="11000", receipt_number="R-1", receipt_date="2026-09-01")
    add_receipt_payment(journey, amount="400000", receipt_number="R-2", receipt_date="2026-09-20")
    result = _check(journey)
    assert result.outcome == "FAIL"
    assert Decimal(result.details["difference"]) == Decimal(-104000)
    assert "short of the 3 invoices" in result.reason


def test_dms_and_tally_copies_of_one_sale_with_different_numbers_count_once(journey):
    """Review B1: the DMS and the Tally copy of one vehicle sale may print
    different numbers; one vehicle (VIN) is one sale invoice."""
    add_ready_document(journey, "customer_invoice_dms", invoice_number="DMS-1", vin_number="MA1VIN0001",
                       grand_total_amount="500000")
    add_ready_document(journey, "tax_invoice_tally", invoice_number="TL-1", vin_number="MA1VIN0001",
                       grand_total_amount="500000")
    add_ready_document(journey, "accessory_invoice_dms", invoice_number="ACC/DMS/0091", invoice_date="2026-10-05",
                       grand_total_amount="7457.01")
    add_ready_document(journey, "accessory_invoice_tally", invoice_number="TL-2231", invoice_date="2026-10-05",
                       grand_total_amount="7457.01")
    _delivered(journey, days_ago=30)
    add_receipt_payment(journey, amount="507457.01", receipt_number="R-1", receipt_date="2026-09-01")
    result = _check(journey)
    assert result.outcome == "PASS", result.reason
    assert Decimal(result.details["invoiced"]) == Decimal("507457.01")
    assert len(result.details["invoices"]) == 2


def test_a_tally_invoice_with_its_own_number_is_a_separate_invoice(journey):
    """2026-10-20 (R2): different invoice numbers are different invoices; a
    Tally accessory invoice is no longer dropped because a DMS one exists."""
    add_ready_document(journey, "customer_invoice_dms", invoice_number="INV-1", grand_total_amount="500000")
    add_ready_document(journey, "accessory_invoice_dms", invoice_number="ACC-1", grand_total_amount="7457.01")
    add_ready_document(journey, "accessory_invoice_tally", invoice_number="ACC-2", grand_total_amount="4746.01")
    _delivered(journey, days_ago=30)
    add_receipt_payment(journey, amount="512203.02", receipt_number="R-1", receipt_date="2026-09-01")
    result = _check(journey)
    assert result.outcome == "PASS", result.reason
    assert Decimal(result.details["invoiced"]) == Decimal("512203.02")


def test_two_rupees_apart_is_not_within_tolerance(journey):
    add_ready_document(journey, "customer_invoice_dms", invoice_number="DMS-1", grand_total_amount="100000")
    _delivered(journey, days_ago=30)
    add_receipt_payment(journey, amount="99998", receipt_number="R-1", receipt_date="2026-09-01")
    assert _check(journey).outcome == "FAIL"


def test_only_tally_on_file_counts_the_tally_invoice(journey):
    add_ready_document(journey, "tax_invoice_tally", invoice_number="TL-1", grand_total_amount="100000")
    _delivered(journey, days_ago=30)
    add_receipt_payment(journey, amount="100000", receipt_number="R-1", receipt_date="2026-09-01")
    assert _check(journey).outcome == "PASS"


def test_not_compared_until_the_settlement_window_closes(journey):
    _bill(journey)
    _delivered(journey, days_ago=2)
    add_receipt_payment(journey, amount="11000", receipt_number="R-1", receipt_date="2026-09-01")
    result = _check(journey)
    assert result.outcome == "SKIPPED"
    assert "settlement window" in result.reason


def test_not_compared_without_a_vehicle_invoice(journey):
    add_ready_document(journey, "accessory_invoice_dms", invoice_number="ACC-1", grand_total_amount="20000")
    _delivered(journey, days_ago=30)
    assert _check(journey).outcome == "SKIPPED"


def test_an_invoice_without_a_readable_total_is_not_guessed(journey):
    add_ready_document(journey, "customer_invoice_dms", invoice_number="DMS-1", grand_total_amount="100000")
    add_ready_document(journey, "ew_invoice", invoice_number="EW-1", plan_name="Gold")
    _delivered(journey, days_ago=30)
    add_receipt_payment(journey, amount="100000", receipt_number="R-1", receipt_date="2026-09-01")
    result = _check(journey)
    assert result.outcome == "SKIPPED"
    assert "ew invoice EW-1" in result.reason


def test_a_failing_check_is_one_team_lead_finding_resolved_once_it_passes(journey):
    add_ready_document(journey, "customer_invoice_dms", invoice_number="DMS-1", grand_total_amount="100000")
    _delivered(journey, days_ago=30)
    add_receipt_payment(journey, amount="60000", receipt_number="R-1", receipt_date="2026-09-01")

    def run() -> None:
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            rules.run_p2_audit_rules(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                     stage="DELIVERY", correlation_id="t", triggering_event="SCHEDULED")

    def findings() -> list[str]:
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            return list(connection.execute(
                text("SELECT finding_status FROM auditcore.audit_findings "
                     "WHERE tenant_id=:t AND journey_id=:j AND rule_key='RECEIPTS_EQUAL_INVOICES'"),
                {"t": journey.tenant_id, "j": journey.journey_id},
            ).scalars())

    run()
    run()
    assert findings() == ["OPEN"]
    add_receipt_payment(journey, amount="40000", receipt_number="R-2", receipt_date="2026-09-05")
    run()
    assert findings() == ["RESOLVED"]
