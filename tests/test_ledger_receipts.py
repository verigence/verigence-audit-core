"""Money the customer ledger records as received (product owner, 2026-11-02).

Live: Bhikari Singh's Tally ledger shows the Rs 11,000 booking amount of
23-09 and the Rs 22,75,000 loan of 12-10; neither receipt is in the file. The
booking amount is money received; the loan is already counted from the bank
letter and is never a second payment.
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
)
from test_uc03_account_statement import _ledger, _set_policy, _sync

from audit_core.db import set_tenant_context
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT
from audit_core.uc03_ledger_receipts import money_in

BHIKARI = [  # SHI-SS-261031-005
    {"entry_date": "2026-09-23", "particulars_raw": "ICICI BANK (788005000453)", "voucher_type": "Receipt",
     "voucher_number": "PB/26-27/0436", "credit_amount": 11000, "running_balance": "11,000.00 Cr"},
    {"entry_date": "2026-10-12", "particulars_raw": "ICICI BANK (788005000453)", "voucher_type": "Receipt",
     "voucher_number": "PB/26-27/0642", "credit_amount": 2275000, "running_balance": "22,86,000.00 Cr"},
    {"entry_date": "2026-10-13", "particulars_raw": "CASH- PURI BYPASS", "voucher_type": "Receipt",
     "voucher_number": "PB/26-27/0658", "credit_amount": 200000, "running_balance": "24,86,000.00 Cr"},
    {"entry_date": "2026-10-14", "particulars_raw": "UNIVERSAL SOMPO GENERAL (INS POLICY)",
     "voucher_type": "Journal_NonGST", "voucher_number": "2367/8777732", "debit_amount": 89668,
     "running_balance": "23,96,332.00 Cr"},
    {"entry_date": "2026-10-14", "particulars_raw": "ICICI BANK (788005000453)", "voucher_type": "Receipt",
     "voucher_number": "PB/26-27/0672", "credit_amount": 50000, "running_balance": "24,46,332.00 Cr"},
    {"entry_date": "2026-10-14", "particulars_raw": "Vehicle Sale 40%", "voucher_type": "Vehicle Sale",
     "voucher_number": "INV27C000130", "debit_amount": 2520960, "running_balance": "74,628.00 Dr"},
    {"entry_date": "2026-10-16", "particulars_raw": "SCRAPPAGE BONUS@40%", "voucher_type": "Credit Note_MM",
     "voucher_number": "UOV27C000050", "credit_amount": 20000, "running_balance": "54,628.00 Dr"},
    {"entry_date": "2026-10-20", "particulars_raw": "BISHNU PRASAD JENA T2J99908", "voucher_type": "Journal_NonGST",
     "voucher_number": "TRANS/OCT/1", "credit_amount": 30000, "running_balance": "24,628.00 Dr"},
    {"entry_date": "2026-10-30", "particulars_raw": "ROUND OFF", "voucher_type": "Journal_NonGST",
     "voucher_number": "ROUND/1", "credit_amount": 33, "running_balance": "24,595.00 Dr"},
]


def test_money_in_is_receipts_and_transfers_never_benefits_or_round_off() -> None:
    assert [(p["number"], p["amount"], p["mode"]) for p in money_in(BHIKARI)] == [
        ("PB/26-27/0436", D(11000), "BANK_TRANSFER"), ("PB/26-27/0642", D(2275000), "BANK_TRANSFER"),
        ("PB/26-27/0658", D(200000), "CASH"), ("PB/26-27/0672", D(50000), "BANK_TRANSFER"),
        ("TRANS/OCT/1", D(30000), "OTHERS"),
    ]


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="lrc")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _payments(journey) -> dict:
    from audit_core.uc03_p2_journey360 import payments

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return payments(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def test_a_ledger_receipt_not_in_the_file_is_counted_once_and_the_loan_never_twice(journey) -> None:
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    add_receipt_payment(journey, amount="200000", receipt_number="PB/26-27/0658", receipt_date="2026-10-13")
    add_receipt_payment(journey, amount="50000", receipt_number="B/26-27/0672", receipt_date="2026-10-14")
    add_ready_document(journey, "bank_approval_letter", process_area="DELIVERY",
                       finance_sanctioned_amount="2275000", financier_name="STATE BANK OF INDIA",
                       finance_sanction_date="2026-10-10")
    _ledger(journey, BHIKARI)
    _sync(journey)
    view = _payments(journey)
    ledger = [i for i in view["items"] if i.get("fromLedger")]
    # The booking amount and the relative's transfer, from the ledger; the cash
    # and the 50,000 stand on their own receipts; the loan is not a payment.
    assert sorted((i["receiptNumber"], D(i["amount"])) for i in ledger) == [
        ("PB/26-27/0436", D(11000)), ("TRANS/OCT/1", D(30000))]
    assert all(i["counted"] for i in ledger)
    assert D(view["receiptsTotal"]) == D(291000)

    # Synced again: nothing added twice.
    _sync(journey)
    assert D(_payments(journey)["receiptsTotal"]) == D(291000)
