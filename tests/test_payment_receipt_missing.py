"""PAYMENT_RECEIPT_MISSING -- money the NDC shows received with no payment
receipt on file raises a PC task; the loan line raises none and is a system
remark instead (product owner 2026-11-16).

Live: SHI-SS-261027-006 -- the NDC's PB/26-27/0575 Rs 21,000 has no receipt
on file (a task); the HDFC credit of Rs 18,80,226 is the loan (a remark)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from audit_core import uc03_account_statement as statement
from audit_core import uc03_ledger_receipts as ledger
from audit_core import uc03_p2_audit_rules as rules
from audit_core import uc03_system_remarks as remarks

LOAN = Decimal(1880226)
RECEIPT = {"number": "PB/26-27/0575", "amount": Decimal(21000), "date": date(2026, 10, 27)}
LOAN_LINE = {"number": None, "amount": LOAN, "date": date(2026, 10, 29)}


@pytest.fixture
def ndc(monkeypatch):
    """An NDC with a customer payment and the loan, nothing on file."""
    state = {"lines": [dict(RECEIPT), dict(LOAN_LINE)], "loans": [LOAN], "receipts": []}
    monkeypatch.setattr(statement, "load_ledger_documents", lambda *a, **k: [])
    monkeypatch.setattr(statement, "journey_statement_received_lines", lambda *a, **k: (
        [dict(line) for line in state["lines"]], sum(line["amount"] for line in state["lines"]), "doc-ndc"))
    monkeypatch.setattr(ledger, "_on_file", lambda *a, **k: list(state["receipts"]))
    monkeypatch.setattr(ledger, "_loan_amounts", lambda *a, **k: list(state["loans"]))
    return state


def _postings():
    return ledger.unreceipted_postings(None, tenant_id="t", journey_id=uuid4())


def test_the_customer_payment_is_asked_for_and_the_loan_is_not(ndc):
    postings = _postings()
    assert [p["amount"] for p in postings["customer"]] == [Decimal(21000)]
    assert postings["customer"][0]["documentLabel"] == "NDC (no receipt on file)"
    assert [p["amount"] for p in postings["loan"]] == [LOAN]
    assert postings["loan"][0]["documentLabel"] == "NDC"


def test_the_loan_net_of_the_lenders_charges_is_still_the_loan(ndc):
    ndc["lines"][1]["amount"] = Decimal(1872000)  # a little under the sanction
    postings = _postings()
    assert [p["amount"] for p in postings["loan"]] == [Decimal(1872000)]
    assert [p["amount"] for p in postings["customer"]] == [Decimal(21000)]


def test_with_no_loan_figure_known_every_line_is_asked_for(ndc):
    ndc["loans"] = []
    postings = _postings()
    assert sorted(p["amount"] for p in postings["customer"]) == [Decimal(21000), LOAN]
    assert postings["loan"] == []


def test_a_receipt_on_file_settles_its_line(ndc):
    ndc["receipts"] = [{"receipt_number": "PB/26-27/0575", "receipt_date": date(2026, 10, 27),
                        "amount": Decimal(21000)}]
    ndc["lines"][1]["amount"] = LOAN
    postings = _postings()
    assert postings["customer"] == []
    assert [p["amount"] for p in postings["loan"]] == [LOAN]


def test_a_loan_receipt_on_file_leaves_no_remark(ndc):
    ndc["receipts"] = [{"receipt_number": "DISB-1", "receipt_date": date(2026, 10, 29), "amount": LOAN}]
    assert _postings()["loan"] == []


class _Facts:
    connection = None
    tenant_id = "t"
    journey_id = uuid4()


def test_the_rule_fails_listing_each_line_and_passes_once_settled(ndc):
    outcome = rules.payment_receipt_missing(_Facts())
    assert outcome.code == "PAYMENT_RECEIPT_MISSING" and outcome.outcome == "FAIL"
    assert "PB/26-27/0575" in outcome.reason and "21,000" in outcome.reason
    assert "18,80,226" not in outcome.reason  # the loan is not asked for
    assert outcome.details["findingTitle"] == "Payment receipt missing"
    assert [p["number"] for p in outcome.details["postings"]] == ["PB/26-27/0575"]

    ndc["lines"][0]["date"] = None  # the NDC prints no date: none is made up
    assert "unknown date" not in rules.payment_receipt_missing(_Facts()).reason

    ndc["lines"] = [dict(LOAN_LINE)]
    assert rules.payment_receipt_missing(_Facts()).outcome == "PASS"


def test_the_loan_line_is_a_system_remark(ndc):
    found = remarks._loan_remarks(None, tenant_id="t", journey_id=uuid4())
    assert len(found) == 1
    remark = found[0]
    assert remark["code"] == "LOAN_WITHOUT_RECEIPT" and remark["source"] == "NDC"
    assert "18,80,226" in remark["text"] and "as per the NDC" in remark["text"]
    assert "no payment receipt available" in remark["text"]


def test_the_rule_is_registered_as_a_pc_task():
    from audit_core.uc03_p2_document_task_groups import document_category
    from audit_core.uc03_p2_registry import get_registry

    assert "PAYMENT_RECEIPT_MISSING" in rules.RULE_CODES["BOOKING"]
    control = get_registry().controls["PAYMENT_RECEIPT_MISSING"]
    assert control.executor == "NATIVE" and control.mode == "RERUN"
    assert document_category("PC_CONFIRMATION", "PROCESS_CONFIRMATION",
                             "PAYMENT_RECEIPT_MISSING") == "MISSING_DOCUMENT"
