"""The customer ledger is a billing document (product owner, 2026-11-02).

Its charge postings (insurance, RTO, accessories, FASTag, warranty, RSA) and
its credit notes are lines of the deal, below the NDC and the cost sheet and
above the invoices. Its vehicle sale (one net figure) is only checked against
the NDC. Rows are from live Tally ledgers (Shivnath, 2 Nov 2026).
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest
from conftest import delete_tenant_data
from p2_support import add_ready_document, create_p2_journey, database_engine
from test_uc03_account_statement import _ledger, _set_policy, _sync

from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import read_tally_lines
from audit_core.uc03_deal_source_policy import POLICY_ACCOUNT_STATEMENT

BAPU = [  # SHI-SS-261031-008
    {"entry_date": "2026-10-15", "particulars_raw": "ICICI BANK (788005000453)", "voucher_type": "Receipt",
     "voucher_number": "PB/26-27/0759", "credit_amount": 11000, "running_balance": "11,000.00 Cr"},
    {"entry_date": "2026-10-16", "particulars_raw": "Vehicle Sale 40%", "voucher_type": "Vehicle Sale",
     "voucher_number": "INV27C000142", "debit_amount": 1735583, "running_balance": "7,64,583.00 Dr"},
    {"entry_date": "2026-10-16", "particulars_raw": "SCRAPPAGE BONUS@40%", "voucher_type": "Credit Note_MM",
     "voucher_number": "UOV27C000046", "credit_amount": 20000, "running_balance": "7,44,583.00 Dr"},
    {"entry_date": "2026-10-23", "particulars_raw": "RTO CHARGES BBSR", "voucher_type": "Journal_NonGST",
     "voucher_number": "RTO/PB/T2J99160", "debit_amount": 132770, "running_balance": "1,22,647.00 Cr"},
    {"entry_date": "2026-10-23", "particulars_raw": "Sale Of Accessories BBSR", "voucher_type": "Journal_NonGST",
     "voucher_number": "AS/PB/T2J99160", "debit_amount": 48398, "running_balance": "74,249.00 Cr"},
    {"entry_date": "2026-10-23", "particulars_raw": "ROYAL SUNDARAM GENERAL INSURANCE ( INS POLICY)",
     "voucher_type": "Journal_NonGST", "voucher_number": "MMBL126529000100", "debit_amount": 72391,
     "running_balance": "1,858.00 Cr"},
    {"entry_date": "2026-10-24", "particulars_raw": "FAST TAG (SALES)", "voucher_type": "Sales_Inv",
     "voucher_number": "FT/PB/0040", "debit_amount": 600, "running_balance": "1,258.00 Cr"},
    {"entry_date": "2026-10-24", "particulars_raw": "ADDITIONAL DISCOUNT@40%", "voucher_type": "Credit Note_MM",
     "voucher_number": "UOV27C000047", "credit_amount": 5000.01, "running_balance": "6,258.01 Cr"},
    {"entry_date": "2026-10-24", "particulars_raw": "ROUND OFF", "voucher_type": "Journal_NonGST",
     "voucher_number": "ROUND/1", "debit_amount": 1, "running_balance": "6,257.01 Cr"},
]


def test_the_ledger_postings_are_read_as_lines_and_benefits() -> None:
    read = read_tally_lines(BAPU)
    assert read.components == {"road_tax_registration": D(132770), "accessories_cost": D(48398),
                               "insurance_amount": D(72391), "fastag_amount": D(600)}
    assert read.discounts == {"SCRAPPAGE_BONUS_DEALER": D(20000), "ADDITIONAL_DISCOUNT": D("5000.01")}
    assert read.credit_notes == {"UOV27C000046": "SCRAPPAGE_BONUS_DEALER", "UOV27C000047": "ADDITIONAL_DISCOUNT"}


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="tly")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _deal(journey) -> dict:
    from audit_core.uc03_p2_journey360 import deal

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return deal(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _row(view: dict, key: str) -> dict:
    return next(r for g in view["categories"] for r in g["components"] if r["key"] == key)


def _benefit(view: dict, key: str) -> dict:
    return next(d for d in view["discounts"] if d["key"] == key)


def test_the_ledger_prices_the_lines_no_ndc_gives_and_confirms_the_insurance(journey) -> None:
    """Bapu Dalabehera: no NDC read (it was filed as a booking docket), the
    Tally ledger on file. Its insurance, RTO, accessories and FASTag are the
    deal's; the insurance is in-house because the dealer charged it; the
    scrappage credit note is the scrappage bonus, counted once."""
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, BAPU)
    _sync(journey)
    add_ready_document(journey, "insurance_cover", process_area="DELIVERY", premium_amount="72391",
                       insurer_name="ROYAL SUNDARAM", policy_number="P-9")
    add_ready_document(journey, "credit_note", process_area="DELIVERY", invoice_number="UOV27C000046",
                       invoice_date="2026-10-16", grand_total_amount="20000")
    view = _deal(journey)
    for key, amount in (("insurance_amount", 72391), ("road_tax_registration", 132770),
                        ("accessories_cost", 48398), ("fastag_amount", 600)):
        row = _row(view, key)
        assert D(row["current"]) == D(amount), key
        assert row["sources"][0]["documentType"] == "customer_ledger_tally", key
    assert view["insurance"]["decidedBy"] == "DOCUMENT" and view["insurance"]["inDeal"] is True
    scrappage = _benefit(view, "SCRAPPAGE_BONUS_DEALER")
    assert D(scrappage["current"]) == D(20000)
    assert [c["number"] for c in scrappage["paidByCreditNotes"]] == ["UOV27C000046"]
    assert D(_benefit(view, "ADDITIONAL_DISCOUNT")["current"]) == D("5000.01")
    assert D(view["summary"]["discounts"]["current"]) == D("25000.01")


def test_the_offer_letter_payment_details_form_is_an_ndc() -> None:
    """Live 2026-11-03: a dealer's "OFFER LETTER / PAYMENT DETAILS" form is its
    NDC; the rows as the document reader returns them (made-up figures)."""
    from decimal import Decimal as D

    from audit_core.uc03_account_statement import (
        KIND_FORM,
        read_statement,
        statement_kind,
    )

    rows = [
        {"particulars_raw": "VEHICLE PRICE", "debit_amount": 1500000},
        {"particulars_raw": "EX SHOWROOM (1-2)", "debit_amount": 1500000},
        {"particulars_raw": "TCS", "debit_amount": 15000},
        {"particulars_raw": "INSURANCE AMOUNT", "debit_amount": 60000},
        {"particulars_raw": "TEMPORARY REGISTRATION", "debit_amount": 500},
        {"particulars_raw": "RTO", "debit_amount": 120000},
        {"particulars_raw": "ACCESSORIES Free", "debit_amount": 25000},
        {"particulars_raw": "PERMIT Add Basic", "debit_amount": 10000},
        {"particulars_raw": "ONROAD PRICE", "debit_amount": 1705500},
        {"particulars_raw": "BALANCE PAYMENT", "debit_amount": 500},
        {"particulars_raw": "EXCHANGE VEHICLE AMT", "credit_amount": 300000},
        {"particulars_raw": "EXCHANGE/LOYALTY BONUS", "credit_amount": 20000},
        {"particulars_raw": "ADDITIONAL DISCOUNT", "credit_amount": 5000},
        {"particulars_raw": "BOOKING AMOUNT", "credit_amount": 21000},
        {"particulars_raw": "FINANCE AMOUNT", "credit_amount": 900000},
        {"particulars_raw": "SUB TOTAL(17+18+19+20+21+22)", "credit_amount": 1246000},
        {"particulars_raw": "FINAL PAYMENT", "credit_amount": 1705000},
        {"entry_date": "2026-10-15", "particulars_raw": "RECEIPT PB/26-27/0759", "voucher_number": "PB/26-27/0759",
         "credit_amount": 21000, "voucher_type": "RTGS"},
    ]
    assert statement_kind(rows) == KIND_FORM
    read = read_statement(rows)
    assert read.components == {"ex_showroom_price": D(1500000), "tcs_amount": D(15000), "insurance_amount": D(60000),
                               "registration_charges": D(120000), "accessories_cost": D(25000),
                               "other_charges": D(10500)}
    assert read.discounts == {"ACCESSORIES_KIT": D(25000), "EXCHANGE_BONUS": D(20000),
                              "ADDITIONAL_DISCOUNT": D(5000)}
    assert (read.financer, read.printed_total, read.old_vehicle_value) == (D(900000), D(1705500), D(300000))
    assert read.unmapped == [] and read.conflicts == []
