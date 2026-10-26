"""RECEIPTS_EQUAL_INVOICES for a Project whose account statement is the source of
truth (product owner, 2026-10-26): the payment receipts are compared with the
"Net value" / "Final price" the statement prints, used as printed. The statement's own received lines are not used."""
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
        assert statement_total({}, read) == D(1145000)


def test_the_three_dealers_total_labels_as_written_on_the_forms() -> None:
    """The three labels on the forms: 'NET RECEIVABLE (A+B)' with the amount
    closed by '/-', and 'Net Value' twice."""
    for label, amount, expected in (("NET RECEIVABLE (A+B)", "2045867/-", D(2045867)),
                                    ("Net Value", 1168095, D(1168095)),
                                    ("Net Value", "32,35,940", D(3235940))):
        read = read_statement([*CHARGES, {"particulars_raw": label, "debit_amount": amount}])
        assert read.printed_total == expected, label
        assert read.unmapped == [] and read.conflicts == []


def test_two_different_printed_totals_are_a_conflict() -> None:
    read = read_statement([*CHARGES, {"particulars_raw": "Net value", "debit_amount": 1145000},
                           {"particulars_raw": "Final price", "debit_amount": 1140000}])
    assert read.printed_total is None
    assert read.conflicts == [{"kind": "TOTAL", "key": "printed_total", "amounts": [D(1140000), D(1145000)]}]


def test_no_printed_total_means_no_total_nothing_is_calculated() -> None:
    assert statement_total({}, read_statement(CHARGES)) is None


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


def test_a_statement_with_no_net_value_is_skipped_not_calculated(journey) -> None:
    _statement(journey, CHARGES)
    _delivered(journey, 30)
    add_receipt_payment(journey, amount="1145000", receipt_number="R-1", receipt_date="2026-09-01")
    result = _check(journey)
    assert result.outcome == "SKIPPED" and "no net value or final price" in result.reason


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


GENERIC_FORM = [  # the generic Account Statement form as DI may read it: charges, receipts and the summary rows
    {"particulars_raw": "Ex Showroom", "debit_amount": 3070050}, {"particulars_raw": "Fast Tag", "debit_amount": 600},
    {"particulars_raw": "Cash", "credit_amount": 25000}, {"particulars_raw": "Online transfer", "credit_amount": 3235715},
    {"particulars_raw": "Gross Value", "debit_amount": 3240650}, {"particulars_raw": "Less: Discount*", "credit_amount": 4710},
    {"particulars_raw": "Total Receipts", "credit_amount": 3260715}, {"particulars_raw": "Short / Excess", "debit_amount": 1},
    {"particulars_raw": "Total Cost", "debit_amount": 3240650}, {"particulars_raw": "TOTAL (A)", "debit_amount": 1},
    {"particulars_raw": "NET AMOUNT RECEIVED (E)", "credit_amount": 3260715},
    {"particulars_raw": "BALANCE EXCESS/SHORTAGE (C-E)", "debit_amount": 503}, {"particulars_raw": "Due", "debit_amount": 45},
]


def test_the_forms_own_summary_rows_are_not_unmapped_lines() -> None:
    read = read_statement([*GENERIC_FORM, {"particulars_raw": "Net Value", "debit_amount": 3235940}])
    assert read.unmapped == [] and read.conflicts == []
    assert read.components == {"ex_showroom_price": D(3070050), "fastag_amount": D(600)}
    assert read.receipts == D(25000 + 3235715)  # cash + online only, never the total
    assert read.printed_total == D(3235940)


def test_the_dedicated_net_value_field_wins_over_the_row() -> None:
    read = read_statement([*CHARGES, {"particulars_raw": "Net value", "debit_amount": 1145000}])
    assert statement_total({"net_value": "11,68,095"}, read) == D(1168095)
    assert statement_total({"net_value": None}, read) == D(1145000)
    assert statement_total({"net_value": "0"}, read) == D(1145000)


def test_a_statement_with_the_net_value_only_in_its_field_is_compared(journey) -> None:
    document_id = uuid4()
    add_evidence(journey, di_document_id=document_id, document_type_key="customer_ledger", process_area="DELIVERY")
    add_extracted_field(journey, di_document_id=document_id, field_key="line_items", value=GENERIC_FORM,
                        document_type="customer_ledger")
    add_extracted_field(journey, di_document_id=document_id, field_key="net_value", value="3235940",
                        document_type="customer_ledger")
    _delivered(journey, 30)
    add_receipt_payment(journey, amount="3235940", receipt_number="R-1", receipt_date="2026-09-01")
    result = _check(journey)
    assert result.outcome == "PASS", result.reason
    assert D(result.details["statementTotal"]) == D(3235940)
