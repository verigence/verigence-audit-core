"""RECEIPTS_EQUAL_INVOICES for a Project whose account statement is the source of
truth (product owner, 2026-10-26): the payment receipts are compared with the
"Net value" / "Final price" the statement prints, or, when it prints none, the
charges less the discounts. The statement's own received lines are not used."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_extracted_field,
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_audit_rules as rules
from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import read_statement, statement_total

D = Decimal
CHARGES = [
    {"particulars_raw": "EX-SHOW ROOM PRICE OF VEHICLE", "debit_amount": 1000000},
    {"particulars_raw": "TCS ON INVOICE", "debit_amount": 10000},
    {"particulars_raw": "INSURANCE COST", "debit_amount": 40000},
    {"particulars_raw": "REGISTRATION COST", "debit_amount": 100000},
    {"particulars_raw": "CASH DISCOUNT", "credit_amount": 5000},
    {"particulars_raw": "BOOKING AMOUNT", "credit_amount": 777},  # the statement's own received line: ignored
]


def test_net_value_and_final_price_are_the_printed_total_not_an_unmapped_line() -> None:
    for label in ("Net value", "NET VALUE", "Final Price"):
        read = read_statement([*CHARGES, {"particulars_raw": label, "debit_amount": 1145000}])
        assert read.printed_total == D(1145000)
        assert read.unmapped == [] and read.conflicts == []
        assert statement_total(read)["source"] == "PRINTED"


def test_two_different_printed_totals_are_a_conflict() -> None:
    read = read_statement([*CHARGES, {"particulars_raw": "Net value", "debit_amount": 1145000},
                           {"particulars_raw": "Final price", "debit_amount": 1140000}])
    assert read.printed_total is None
    assert read.conflicts == [{"kind": "TOTAL", "key": "printed_total", "amounts": [D(1140000), D(1145000)]}]


def test_without_a_printed_total_it_is_the_charges_less_the_discounts() -> None:
    total = statement_total(read_statement(CHARGES))
    assert total["source"] == "CHARGES_LESS_DISCOUNTS"
    assert total["total"] == D(1145000) and total["charges"] == D(1150000) and total["discounts"] == D(5000)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="rcpstm")
    with engine.begin() as c:
        set_tenant_context(c, created.tenant_id)
        c.execute(text("UPDATE auditcore.projects SET deal_source_policy='ACCOUNT_STATEMENT' WHERE tenant_id=:t"),
                  {"t": created.tenant_id})
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _statement(journey, lines: list[dict]) -> None:
    document_id = uuid4()
    add_evidence(journey, di_document_id=document_id, document_type_key="customer_ledger", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=document_id, field_key="line_items", value=lines,
                        document_type="customer_ledger")


def _delivered(journey, days_ago: int) -> None:
    day = (datetime.now(UTC).date() - timedelta(days=days_ago)).isoformat()
    add_ready_document(journey, "gate_pass", delivery_date=day)


def _check(journey) -> rules.RuleOutcome:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return rules.receipts_equal_invoices(rules._Facts(connection, journey.tenant_id, journey.journey_id))


def test_receipts_equal_to_the_net_value_pass(journey) -> None:
    _statement(journey, [*CHARGES, {"particulars_raw": "Net value", "debit_amount": 1145000}])
    _delivered(journey, 30)
    add_receipt_payment(journey, amount="45000", receipt_number="R-1", receipt_date="2026-09-01")
    add_receipt_payment(journey, amount="1100000.50", receipt_number="R-2", receipt_date="2026-09-20")
    result = _check(journey)
    assert result.outcome == "PASS", result.reason
    assert "net value printed on the account statement" in result.reason
    assert D(result.details["statementTotal"]) == D(1145000) and D(result.details["difference"]) == D("0.50")
    assert D(result.details["statementReceivedLines"]) == D(777)  # shown, never compared


def test_receipts_short_of_the_final_price_fail_with_the_gap(journey) -> None:
    _statement(journey, [*CHARGES, {"particulars_raw": "Final price", "debit_amount": 1145000}])
    _delivered(journey, 30)
    add_receipt_payment(journey, amount="1100000", receipt_number="R-1", receipt_date="2026-09-01")
    result = _check(journey)
    assert result.outcome == "FAIL"
    assert D(result.details["difference"]) == D(-45000)
    assert result.details["findingTitle"] == "Receipts do not equal the account statement"


def test_with_no_printed_total_the_discounts_are_subtracted(journey) -> None:
    _statement(journey, CHARGES)
    _delivered(journey, 30)
    add_receipt_payment(journey, amount="1145000", receipt_number="R-1", receipt_date="2026-09-01")
    result = _check(journey)
    assert result.outcome == "PASS", result.reason
    assert result.details["statementTotalSource"] == "CHARGES_LESS_DISCOUNTS"


def test_skipped_without_a_statement_or_inside_the_settlement_window(journey) -> None:
    assert _check(journey).outcome == "SKIPPED"
    _statement(journey, [*CHARGES, {"particulars_raw": "Net value", "debit_amount": 1145000}])
    assert _check(journey).outcome == "SKIPPED"  # no delivery date
    _delivered(journey, 2)
    result = _check(journey)
    assert result.outcome == "SKIPPED" and "settlement window" in result.reason


def test_a_line_that_could_not_be_read_skips_instead_of_guessing(journey) -> None:
    _statement(journey, [*CHARGES, {"particulars_raw": "Mystery fee", "debit_amount": 500},
                         {"particulars_raw": "Net value", "debit_amount": 1145000}])
    _delivered(journey, 30)
    result = _check(journey)
    assert result.outcome == "SKIPPED" and "MYSTERY FEE" in result.reason
