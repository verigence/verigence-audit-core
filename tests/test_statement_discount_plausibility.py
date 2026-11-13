"""A total or the old vehicle's value read on a discount row is never a
discount (product owner 2026-11-13: the Excess Discount entries revalidated;
Pinki Dixit's Rs 20,52,186 "Other Discount" was the statement's money
received, and UTK-RM-261024-005's Rs 2,30,000 "Exchange Bonus" was the old
car's value). Pure: no database."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from uuid import uuid4

from audit_core import uc03_account_statement as module

NOW = datetime(2026, 11, 13, tzinfo=UTC)

# UTK-SB-261106-007 as read on 13 Nov.
PINKI = [
    {"particulars_raw": "Vehicle cost", "debit_amount": 1797000},
    {"particulars_raw": "TCS", "debit_amount": 17970},
    {"particulars_raw": "Insurance amount", "debit_amount": 101205},
    {"particulars_raw": "Registration", "debit_amount": 136415},
    {"particulars_raw": "Fastag charges", "debit_amount": 600},
    {"particulars_raw": "FINANCE D/O", "credit_amount": 1190186},
    *({"particulars_raw": f"MR NO {n}", "credit_amount": a} for n, a in (
        ("0226", 199000), ("1564", 21000), ("0159", 30000), ("0253", 400000), ("1028", 21000),
        ("0254", 50000), ("0245", 50000), ("0329", 82000), ("0327", 9000))),
    {"particulars_raw": "Other Discount (if any)", "credit_amount": 2052186},
]


def _doc(rows: list[dict], minutes: int = 0) -> tuple[dict, list[dict]]:
    return ({"documentId": uuid4(), "evidenceId": uuid4(), "linkedAtUtc": NOW, "fields": {}}, rows)


def test_the_money_received_read_on_the_other_discount_row_is_not_a_discount() -> None:
    read = module.read_statement(PINKI)
    assert "ADDITIONAL_DISCOUNT" not in read.discounts
    assert read.components["ex_showroom_price"] == D(1797000)
    assert any(u["label"] == "ADDITIONAL_DISCOUNT" and u["amount"] == D(2052186) for u in read.unmapped)


def test_a_real_other_discount_is_still_read() -> None:
    rows = [*PINKI[:-1], {"particulars_raw": "Other Discount (if any)", "credit_amount": 10000}]
    assert module.read_statement(rows).discounts["ADDITIONAL_DISCOUNT"] == D(10000)


def test_half_the_charges_or_more_on_a_discount_row_is_a_total() -> None:
    rows = [{"particulars_raw": "Vehicle cost", "debit_amount": 900000},
            {"particulars_raw": "Scheme Discount", "credit_amount": 905000}]
    assert "CASH_DISCOUNT" not in module.read_statement(rows).discounts


def test_the_old_vehicle_value_on_another_copy_is_not_an_exchange_bonus() -> None:
    utkal = [{"particulars_raw": "Vehicle cost", "debit_amount": 1300000},
             {"particulars_raw": "Exchange Value", "credit_amount": 230000},
             {"particulars_raw": "Exchange Loyalty", "credit_amount": 20000}]
    other = [{"particulars_raw": "Vehicle cost", "debit_amount": 1300000},
             {"particulars_raw": "Exchange Bonus", "credit_amount": 230000, "discount_detail": True}]
    statement, combined = module.combine_statements([_doc(other), _doc(utkal)])
    assert statement is not None
    assert statement.discounts.get("EXCHANGE_BONUS") != D(230000)
    assert statement.old_vehicle_value == D(230000)
    assert ("DISCOUNT", "EXCHANGE_BONUS") not in combined.figures or combined.figures[("DISCOUNT", "EXCHANGE_BONUS")] == D(20000)
