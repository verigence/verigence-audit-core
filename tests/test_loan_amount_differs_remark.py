"""LOAN_AMOUNT_DIFFERS -- the Delivery Order and the Bank Approval Letter print different loan
amounts: a system remark (product owner 2026-11-17)."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from audit_core import uc03_p2_journey360 as j360
from audit_core import uc03_system_remarks as remarks


@pytest.fixture
def loans(monkeypatch):
    state = {"do": Decimal(850000), "letter": Decimal(900000)}
    monkeypatch.setattr(j360, "delivery_order_loan", lambda *a, **k: (
        None if state["do"] is None else {"amount": state["do"]}))
    monkeypatch.setattr(j360, "bank_approval_sanction", lambda *a, **k: (
        None if state["letter"] is None else {"amount": state["letter"]}))
    return state


def _found():
    return remarks._loan_amount_remarks(None, tenant_id="t", journey_id=uuid4())


def test_different_amounts_are_a_remark_naming_both(loans):
    [remark] = _found()
    assert remark["code"] == "LOAN_AMOUNT_DIFFERS"
    assert "8,50,000" in remark["text"] and "9,00,000" in remark["text"] and "50,000" in remark["text"]
    assert "lender's charges" not in remark["text"]


def test_a_small_difference_says_it_may_be_the_lenders_charges(loans):
    loans["do"] = Decimal(891000)
    [remark] = _found()
    assert "lender's charges" in remark["text"]


def test_the_same_amount_or_one_document_is_no_remark(loans):
    loans["do"] = Decimal(900000)
    assert _found() == []
    loans["do"], loans["letter"] = Decimal(850000), None
    assert _found() == []


def test_a_written_slash_dash_read_as_one_is_found() -> None:
    """2026-11-17, UTK-SB-261030-009: '30000/-' read as 30001."""
    from audit_core.uc03_statement_reread import slash_dash_amounts

    fields = {"discount_details": '[{"label": "Scheme Discount", "amount": 30001}, {"label": "Exchange", "amount": 20001}]',
              "line_items": [{"particulars_raw": "Insurance amount", "debit_amount": 34993},
                             {"particulars_raw": "Registration", "debit_amount": 61001}]}
    assert slash_dash_amounts(fields) == [Decimal(30001), Decimal(20001), Decimal(61001)]
    assert slash_dash_amounts({"discount_details": [{"amount": 30000}], "line_items": []}) == []
