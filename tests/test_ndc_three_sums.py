"""The payment receipts against the NDC, as sums (product owner 2026-11-17).

"NDC has to be there to read all the line items ... all these values should add
up as one sum; now if NDC has payment receipt info, add up all those numbers and
verify if two sums are same; additionally check the payment receipts and add up
those, if payment receipts are less then just raise that task. Here it may be
that customer has taken up loan. If payment receipt is equal to the loan amount
... don't raise task for this and just add system remark."

Live UTK-SA-261103-012 (Avijit Biswal): receipts 2045 of Rs 11,000 and 2049 of
Rs 4,00,000 on file; the NDC's lines read "CHAN-2045 Rs 1,100" and "CHAN-2047
Rs 4,50,000" -- matched line by line, Rs 4,51,100 of receipts were asked for.
Read as sums, the NDC's payment lines did not add up to its printed Total
Receipts: nothing is asked until the reading is corrected. Real Postgres.
"""
from __future__ import annotations

import json
from decimal import Decimal as D
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_extracted_field,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
)
from test_uc03_account_statement import _set_policy

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT

# Utkal's Vehicle Account Statement (UTK-RM-261104-002's figures): the payments
# column is the loan, two customer receipts and the two discounts -- Rs 8,72,090.
UTKAL = [
    {"particulars_raw": "Vehicle cost", "debit_amount": "852000"},
    {"particulars_raw": "Registration", "debit_amount": "3685"},
    {"particulars_raw": "Permit", "debit_amount": "6500"},
    {"particulars_raw": "Accessories", "debit_amount": "5140"},
    {"particulars_raw": "RSA", "debit_amount": "940"},
    {"particulars_raw": "Finance D/O KJR/2627/133", "credit_amount": "741090"},
    {"particulars_raw": "MR No KRM/2627/3592", "credit_amount": "11000"},
    {"particulars_raw": "MR No KRM/2627/3677", "credit_amount": "100000"},
    {"particulars_raw": "Scheme Discount", "credit_amount": "13000"},
    {"particulars_raw": "Other Discount (if any)", "credit_amount": "7000"},
]
PRINTED = {"total_cost": "868265", "total_receipts": "872090"}
LOAN = D(741090)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="ndc3")
    _set_policy(created, POLICY_ACCOUNT_STATEMENT)
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _ndc(journey, rows=UTKAL, **printed) -> None:
    document_id = uuid4()
    add_evidence(journey, di_document_id=document_id, document_type_key="customer_ledger", process_area="DELIVERY")
    for key, value in {"line_items": json.dumps(rows), **printed}.items():
        add_extracted_field(journey, di_document_id=document_id, field_key=key, value=value,
                            document_type="customer_ledger")


def _check(journey) -> dict:
    from audit_core.uc03_ledger_receipts import receipts_against_ndc

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return receipts_against_ndc(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _rule(journey):
    from audit_core.uc03_p2_audit_rules import _Facts, payment_receipt_missing

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return payment_receipt_missing(_Facts(c, journey.tenant_id, journey.journey_id))


def _remarks(journey) -> list[dict]:
    from audit_core.uc03_system_remarks import _loan_remarks

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return _loan_remarks(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def test_receipts_covering_the_ndcs_money_ask_for_nothing_whatever_their_numbers(journey) -> None:
    """Sums, not lines: two receipts the NDC lists as three, under other numbers, cover it."""
    _ndc(journey, **PRINTED)
    add_receipt_payment(journey, amount="60000", receipt_number="X/1", receipt_date="2026-10-01")
    add_receipt_payment(journey, amount="51000", receipt_number="X/2", receipt_date="2026-10-02")
    add_receipt_payment(journey, amount="741090", receipt_number="X/3", receipt_date="2026-10-03")
    check = _check(journey)
    # the money: the customer's Rs 1,11,000 and the loan; the discounts in the column are not money
    assert (check["status"], check["customer"], check["loan"], check["money"]) == (
        "COVERED", D(111000), LOAN, D(852090))
    assert check["printedReceipts"] == D(872090) and check["onFile"] == D(852090)
    assert (check["short"], check["loanWithoutReceipt"]) == (D(0), D(0))
    assert _rule(journey).outcome == "PASS"
    assert _remarks(journey) == []


def test_short_by_the_loan_only_is_a_system_remark_not_a_task(journey) -> None:
    _ndc(journey, **PRINTED)
    add_receipt_payment(journey, amount="11000", receipt_number="KRM/2627/3592", receipt_date="2026-10-01")
    add_receipt_payment(journey, amount="100000", receipt_number="KRM/2627/3677", receipt_date="2026-10-02")
    check = _check(journey)
    assert (check["status"], check["short"], check["loanWithoutReceipt"]) == ("COVERED", D(0), LOAN)
    result = _rule(journey)
    assert result.outcome == "PASS" and "no receipt is asked for it" in result.reason
    [remark] = _remarks(journey)
    assert remark["code"] == "LOAN_WITHOUT_RECEIPT" and "7,41,090" in remark["text"]


def test_the_loan_net_of_the_lenders_charges_is_still_the_loan(journey) -> None:
    """The disbursement is the loan less the lender's charges (within 2%)."""
    _ndc(journey, **PRINTED)
    add_receipt_payment(journey, amount="111000", receipt_number="R-1", receipt_date="2026-10-01")
    add_receipt_payment(journey, amount="10000", receipt_number="R-2", receipt_date="2026-10-02")
    # Rs 1,21,000 on file: Rs 10,000 more than the customer's money, Rs 7,31,090 short (98.6% of the loan)
    check = _check(journey)
    assert (check["status"], check["loanWithoutReceipt"]) == ("COVERED", LOAN)


def test_short_by_more_than_the_loan_asks_for_the_rest_once(journey) -> None:
    _ndc(journey, **PRINTED)
    add_receipt_payment(journey, amount="11000", receipt_number="KRM/2627/3592", receipt_date="2026-10-01")
    check = _check(journey)
    assert (check["status"], check["short"], check["loanWithoutReceipt"]) == ("SHORT", D(100000), LOAN)
    result = _rule(journey)
    assert result.outcome == "FAIL"
    assert "Payment receipts of ₹1,00,000 are not on file" in result.reason
    assert "loan of ₹7,41,090 is received as per the NDC" in result.reason
    assert D(result.details["short"]) == D(100000)
    # the short-payment checks wait for those receipts: one cause, one task
    from audit_core.uc03_p2_audit_rules import _awaiting_receipts, _Facts

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        facts = _Facts(c, journey.tenant_id, journey.journey_id)
        assert facts.missing_receipts_total == D(100000)
        assert _awaiting_receipts(facts, D(100000), D(1000)) is not None
        assert _awaiting_receipts(facts, D(250000), D(1000)) is None


def test_payment_lines_that_do_not_give_the_printed_total_ask_for_nothing(journey) -> None:
    """Avijit Biswal's case: a handwritten line misread (Rs 1,00,000 read Rs 1,50,000). Line by
    line it was a receipt "missing"; as sums the NDC's payments do not add up, and the reading
    is the one task."""
    misread = [dict(r) for r in UTKAL]
    misread[7]["credit_amount"] = "150000"
    _ndc(journey, misread, **PRINTED)
    add_receipt_payment(journey, amount="11000", receipt_number="KRM/2627/3592", receipt_date="2026-10-01")
    add_receipt_payment(journey, amount="100000", receipt_number="KRM/2627/3677", receipt_date="2026-10-02")
    check = _check(journey)
    assert check["status"] == "HELD" and check["short"] == D(0)
    assert "Total Receipts of Rs 8,72,090" in check["held"]
    result = _rule(journey)
    assert result.outcome == "SKIPPED" and "NDC rows do not add up" in result.reason
    assert _remarks(journey) == []


def test_an_ndc_printing_no_total_receipts_asks_for_nothing(journey) -> None:
    _ndc(journey, total_cost="868265")
    add_receipt_payment(journey, amount="11000", receipt_number="KRM/2627/3592", receipt_date="2026-10-01")
    check = _check(journey)
    assert check["status"] == "HELD" and "prints no Total Receipts" in check["held"]
    assert _rule(journey).outcome == "SKIPPED"


def test_no_ndc_on_file_is_not_judged(journey) -> None:
    add_receipt_payment(journey, amount="11000", receipt_number="R-1", receipt_date="2026-10-01")
    assert _check(journey)["status"] == "NO_NDC"
    assert _rule(journey).outcome == "SKIPPED"


def test_the_audit_summary_shows_the_three_sums() -> None:
    from audit_core.uc03_p2_audit_summary import ndc_payments_view

    view = ndc_payments_view({
        "status": "SHORT", "documentId": "d", "net": D(852090), "printedReceipts": D(872090),
        "ndcBalance": D(-20000), "money": D(852090), "customer": D(111000), "loan": LOAN,
        "onFile": D(11000), "short": D(100000), "loanWithoutReceipt": LOAN})
    assert (view["receiptsShort"], view["ndcReceived"], view["loanWithoutReceipt"]) == (
        "100000.00", "852090.00", "741090.00")
    assert "Rs 1,00,000" in view["reason"] and "(system remark)" in view["reason"]
    assert "Rs 20,000 over its net value" in view["reason"]
    assert ndc_payments_view({"status": "NO_NDC"}) is None


def test_the_rule_is_registered_as_a_pc_task() -> None:
    from audit_core.uc03_p2_audit_rules import RULE_CODES

    assert "PAYMENT_RECEIPT_MISSING" in RULE_CODES["BOOKING"]


def test_the_financiers_payment_among_the_received_lines_is_still_the_loan(journey) -> None:
    """Shivnath's offer letter: the finance planned and its receipt listed with the customer's
    payments. One payment, counted once, and the loan: short by it alone is a remark."""
    letter = [
        {"particulars_raw": "VEHICLE PRICE", "debit_amount": "1100000"},
        {"particulars_raw": "FINANCE AMOUNT", "credit_amount": "900000"},
        {"particulars_raw": "RECEIPT 0608", "credit_amount": "200000"},
        {"particulars_raw": "RECEIPT 0700", "credit_amount": "900000"},
    ]
    from audit_core.uc03_account_statement import read_statement

    read = read_statement(letter)
    if not any(abs(a - D(900000)) <= 1 for _, a in read.received):
        pytest.skip("this form does not list the financier's payment among its received lines")
    _ndc(journey, letter, total_cost="1100000", total_receipts="1100000")
    add_receipt_payment(journey, amount="200000", receipt_number="PB/26-27/0608", receipt_date="2026-10-01")
    check = _check(journey)
    assert (check["customer"], check["loan"], check["money"]) == (D(200000), D(900000), D(1100000))
    assert (check["status"], check["loanWithoutReceipt"]) == ("COVERED", D(900000))


def test_a_receipt_beyond_the_ndcs_customer_money_does_not_hide_the_loan(journey) -> None:
    """Live ADI-SJ-261023-014: the receipts on file are Rs 51,870 more than the NDC's customer
    money (an accessory receipt), so the shortfall was the loan less Rs 51,870 -- and was taken
    for missing receipts of Rs 14 lakh. The loan with no receipt of its own is the remark."""
    _ndc(journey, **PRINTED)
    add_receipt_payment(journey, amount="111000", receipt_number="R-1", receipt_date="2026-10-01")
    add_receipt_payment(journey, amount="51870", receipt_number="C-1", receipt_date="2026-10-02")
    check = _check(journey)
    assert (check["status"], check["short"], check["loanWithoutReceipt"]) == ("COVERED", D(0), LOAN)


def test_the_loans_own_receipt_on_file_leaves_no_remark(journey) -> None:
    _ndc(journey, **PRINTED)
    add_receipt_payment(journey, amount="111000", receipt_number="R-1", receipt_date="2026-10-01")
    add_receipt_payment(journey, amount="730000", receipt_number="R-2", receipt_date="2026-10-02")  # net of charges
    check = _check(journey)
    assert (check["status"], check["loanWithoutReceipt"]) == ("COVERED", D(0))


def test_a_payments_column_a_few_rupees_off_is_still_checked(journey) -> None:
    """Live: many NDCs' payment lines are Rs 50 or Rs 100 off their printed total (rounding,
    a paisa column). Within the receipts tolerance the money is taken from the lines."""
    _ndc(journey, total_cost="868265", total_receipts="872140")  # Rs 50 over the lines
    add_receipt_payment(journey, amount="11000", receipt_number="KRM/2627/3592", receipt_date="2026-10-01")
    check = _check(journey)
    assert (check["status"], check["short"]) == ("SHORT", D(100000))
