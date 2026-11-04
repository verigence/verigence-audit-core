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


def test_the_ledgers_commercial_registration_fastag_permit_and_warranty_postings_are_read() -> None:
    """All Tally ledgers, 2026-11-04: Utkal's "COMMERCIAL REGISTRATION
    ACCOUNT-20309400" (13 journeys) and "FASTAG CHARGES" (5), the permit, the
    choice number and "EXTENDED WARRENTY A/C" were debited but not read."""
    rows = [{"particulars_raw": p, "voucher_type": "Journal_NonGST", "debit_amount": a, "running_balance": "1 Dr"}
            for p, a in (("COMMERCIAL REGISTRATION ACCOUNT-20309400", 177514), ("FASTAG CHARGES 17900", 600),
                         ("PERMIT BBSR", 6000), ("CHOICE NUMBER A/C", 11300), ("TR CHARGES PAYABLE", 1000),
                         ("EXTENDED WARRENTY A/C 52802", 22750), ("ROUND OFF", 3))]
    assert read_tally_lines(rows).components == {
        "road_tax_registration": D(177514), "fastag_amount": D(600), "other_charges": D(18300),
        "extended_warranty_amount": D(22750)}


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


def test_the_offer_letter_without_its_ex_showroom_row_reads_the_vehicle_price() -> None:
    """Live 2026-11-03 (SHI-SS-261027-024): the dealer wrote only the vehicle
    price; the Ex Showroom (1-2) row, vehicle price less scheme, was blank, so
    the deal had no price and no payable."""
    from decimal import Decimal as D

    from audit_core.uc03_account_statement import read_statement

    base = [{"particulars_raw": "TCS", "debit_amount": 19285},
            {"particulars_raw": "FINANCE AMOUNT", "credit_amount": 1500000}]
    read = read_statement([{"particulars_raw": "VEHICLE PRICE", "debit_amount": 1928561}, *base])
    assert read.components["ex_showroom_price"] == D(1928561) and read.unmapped == []
    # A scheme is a benefit on the vehicle price (2026-11-03), never taken off the price.
    with_scheme = read_statement([{"particulars_raw": "VEHICLE PRICE", "debit_amount": 1928561},
                                  {"particulars_raw": "SCHEME", "debit_amount": 28561}, *base])
    assert with_scheme.components["ex_showroom_price"] == D(1928561)
    assert with_scheme.discounts == {"CASH_DISCOUNT": D(28561)}
    # An Ex Showroom row that is not the vehicle price less the scheme is read as written.
    printed = read_statement([{"particulars_raw": "VEHICLE PRICE", "debit_amount": 1928561},
                              {"particulars_raw": "EX SHOWROOM (1-2)", "debit_amount": 1900000}, *base])
    assert printed.components["ex_showroom_price"] == D(1900000) and printed.discounts == {}


def test_the_offer_letters_scheme_is_a_benefit_of_the_type_the_letter_names() -> None:
    """Live 2026-11-03 (SHI-SS-261027-007): Vehicle price Rs 9,99,901, Scheme
    Rs 30,000, Ex Showroom (1-2) Rs 9,69,901 and, on the right, "Corporate
    discount" Rs 30,000. The customer paid the Ex Showroom price plus the
    charges: the corporate discount is the scheme, named (product owner: "30k
    is a corporate discount"). Read before as a price Rs 30,000 under the
    price master, with the Rs 30,000 given again."""
    from decimal import Decimal as D

    from audit_core.uc03_account_statement import read_statement

    rows = [{"particulars_raw": "VEHICLE PRICE", "debit_amount": 999901},
            {"particulars_raw": "SCHEME", "debit_amount": 30000},
            {"particulars_raw": "EX SHOWROOM (1-2)", "debit_amount": 969901},
            {"particulars_raw": "INSURANCE AMOUNT", "debit_amount": 39381},
            {"particulars_raw": "ONROAD PRICE", "debit_amount": 1028352},
            {"particulars_raw": "CORPORATE DISCOUNT", "credit_amount": 30000},
            {"particulars_raw": "FINANCE AMOUNT", "credit_amount": 984872}]
    read = read_statement(rows)
    assert read.components["ex_showroom_price"] == D(999901)
    assert read.discounts == {"CORPORATE_PRIVILEGE": D(30000)}
    assert read.unmapped == [] and read.conflicts == []

    # No benefit on the letter has the scheme's amount: it is the scheme discount,
    # beside the others (SHI-SS-261027-025: Scheme Rs 25,000, Corporate Rs 5,000).
    rows = [{"particulars_raw": "VEHICLE PRICE", "debit_amount": 1327501},
            {"particulars_raw": "SCHEME", "debit_amount": 25000},
            {"particulars_raw": "EX SHOWROOM (1-2)", "debit_amount": 1302501},
            {"particulars_raw": "CORPORATE DISCOUNT", "credit_amount": 5000}]
    read = read_statement(rows)
    assert read.components["ex_showroom_price"] == D(1327501)
    assert read.discounts == {"CORPORATE_PRIVILEGE": D(5000), "CASH_DISCOUNT": D(25000)}

    # Two benefits with the scheme's amount: which one it is cannot be told, so
    # the scheme stands as its own (SHI-SS-261101-001: Scheme, Corporate and
    # Additional discount, Rs 5,000 each).
    rows = [{"particulars_raw": "VEHICLE PRICE", "debit_amount": 999901},
            {"particulars_raw": "SCHEME", "credit_amount": 5000},
            {"particulars_raw": "EX SHOWROOM (1-2)", "debit_amount": 994901},
            {"particulars_raw": "CORPORATE DISCOUNT", "credit_amount": 5000},
            {"particulars_raw": "ADDITIONAL DISCOUNT", "credit_amount": 5000}]
    read = read_statement(rows)
    assert read.discounts == {"CORPORATE_PRIVILEGE": D(5000), "ADDITIONAL_DISCOUNT": D(5000),
                              "CASH_DISCOUNT": D(5000)}

    # "Scheme of Month" (another branch's form) is the same row.
    read = read_statement([{"particulars_raw": "Vehicle Price", "debit_amount": 3050000},
                           {"particulars_raw": "Scheme of Month", "debit_amount": -100000},
                           {"particulars_raw": "Ex-Showroom Price (1-2)", "debit_amount": 2950000},
                           {"particulars_raw": "Additional Discount Entry", "credit_amount": 15000}])
    assert read.components["ex_showroom_price"] == D(3050000)
    assert read.discounts == {"ADDITIONAL_DISCOUNT": D(15000), "CASH_DISCOUNT": D(100000)}

    # The three rows do not add up (a figure misread): the Ex Showroom row stands, no scheme.
    read = read_statement([{"particulars_raw": "VEHICLE PRICE", "debit_amount": 1310902},
                           {"particulars_raw": "SCHEME", "debit_amount": 50000},
                           {"particulars_raw": "EX SHOWROOM (1-2)", "debit_amount": 1340902}])
    assert read.components["ex_showroom_price"] == D(1340902) and read.discounts == {}
    assert [u["label"] for u in read.unmapped] == ["SCHEME"]

    # The Ex Showroom row off by more than half is the row misread (SHI-SS-261027-021).
    read = read_statement([{"particulars_raw": "VEHICLE PRICE", "debit_amount": 1928501},
                           {"particulars_raw": "EX SHOWROOM (1-2)", "debit_amount": 19285}])
    assert read.components["ex_showroom_price"] == D(1928501) and read.discounts == {}


def test_a_kit_written_into_another_row_of_the_offer_letter_is_accessories() -> None:
    """Live 2026-11-03 (SHI-SS-261031-008): "ACCESSORIES Free 30,000" and, in
    the PERMIT row, "Add Boss kit 18,398": the kit costs Rs 48,398 (the
    ledger's and the accessory invoice's figure), Rs 30,000 of it free."""
    from decimal import Decimal as D

    from audit_core.uc03_account_statement import read_statement

    read = read_statement([{"particulars_raw": "EX SHOWROOM (1-2)", "debit_amount": 1748401},
                           {"particulars_raw": "ACCESSORIES Free", "debit_amount": 30000},
                           {"particulars_raw": "PERMIT Add cash kit", "debit_amount": 18398},
                           {"particulars_raw": "PERMIT", "debit_amount": 2500}])
    assert read.components["accessories_cost"] == D(48398) and read.components["other_charges"] == D(2500)
    assert read.discounts == {"ACCESSORIES_KIT": D(30000)} and read.conflicts == []


OFFER_LETTER = [  # SHI-SS-261031-008, the Offer Letter read as its NDC
    {"particulars_raw": "VEHICLE PRICE", "debit_amount": 1748401},
    {"particulars_raw": "EX SHOWROOM (1-2)", "debit_amount": 1748401},
    {"particulars_raw": "TCS", "debit_amount": 17484}, {"particulars_raw": "INSURANCE AMOUNT", "debit_amount": 72391},
    {"particulars_raw": "RTO", "debit_amount": 133185}, {"particulars_raw": "ACCESSORIES Free", "debit_amount": 30000},
    {"particulars_raw": "PERMIT Add cash kit", "debit_amount": 18398},
    {"particulars_raw": "EXCHANGE/LOYALTY BONUS", "credit_amount": 20000},
    {"particulars_raw": "BOOKING AMOUNT", "credit_amount": 21000},
    {"particulars_raw": "FINANCE AMOUNT", "credit_amount": 950000},
]


def test_one_benefit_the_ndc_and_the_ledger_name_differently_counts_once(journey) -> None:
    """Live 2026-11-03 (SHI-SS-261031-008): the Offer Letter's "Exchange /
    loyalty bonus" Rs 20,000 and the ledger's credit note "SCRAPPAGE BONUS"
    Rs 20,000 were both counted, the payable Rs 20,000 short. The ledger's
    credit note names the benefit; the NDC's row checks it."""
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, BAPU)
    _ledger(journey, OFFER_LETTER)
    _sync(journey)
    view = _deal(journey)
    exchange, scrappage = _benefit(view, "EXCHANGE_BONUS"), _benefit(view, "SCRAPPAGE_BONUS_DEALER")
    assert exchange["current"] is None and exchange["sameAs"]["key"] == "SCRAPPAGE_BONUS_DEALER"
    assert D(scrappage["current"]) == D(20000)
    assert D(_benefit(view, "ACCESSORIES_KIT")["current"]) == D(30000)
    assert D(view["summary"]["discounts"]["current"]) == D("55000.01")
    # The kit: Rs 30,000 free plus Rs 18,398 written into the PERMIT row.
    assert D(_row(view, "accessories_cost")["current"]) == D(48398)
    # The letter leaves the EW and RSA rows blank and no bill charges them:
    # not taken, never awaited (the settlement was "awaiting the EW and RSA bills").
    awaited = {a["key"] for a in view["summary"]["awaiting"]}
    assert not awaited & {"additional_warranty_amount", "extended_warranty_amount", "rsa_amount"}


def test_offer_letter_rows_for_a_bh_number_and_a_fastag_written_on_others() -> None:
    """Live 2026-11-03: "RTO BH" (SHI-SS-261027-025), "RTO (BH Series)"
    (SHI-SS-261027-007) and "OTHERS FT" / "OTHERS Fastag" (the FASTag written
    on the Others row) were left unmapped or read as other charges."""
    from audit_core.uc03_account_statement import read_statement

    for rto in ("RTO BH", "RTO (BH Series)", "RTO"):
        assert read_statement([{"particulars_raw": rto, "debit_amount": 21000}]).components == {
            "registration_charges": D(21000)}
    for fastag in ("OTHERS FT", "OTHERS Fastag", "FASTAG"):
        read = read_statement([{"particulars_raw": fastag, "debit_amount": 600}])
        assert read.components == {"fastag_amount": D(600)} and read.unmapped == []


def test_one_charge_the_ndc_and_the_ledger_name_differently_counts_once(journey) -> None:
    """Live 2026-11-03 (SHI-SS-261027-010): the Offer Letter's "RSA" row, struck
    out with "Fastag 600" written over it, read as an RSA of Rs 600 beside the
    ledger's FASTag Rs 600: the Rs 600 counted twice."""
    _set_policy(journey, POLICY_ACCOUNT_STATEMENT)
    _ledger(journey, BAPU)  # its FASTag posting: Rs 600
    _ledger(journey, [line if line["particulars_raw"] != "PERMIT Add cash kit" else
                      {"particulars_raw": "RSA", "debit_amount": 600} for line in OFFER_LETTER])
    _sync(journey)
    view = _deal(journey)
    rsa, fastag = _row(view, "rsa_amount"), _row(view, "fastag_amount")
    assert rsa["current"] is None and rsa["sameAs"]["key"] == "fastag_amount"
    assert D(fastag["current"]) == D(600)


def test_a_charge_written_into_its_label_with_no_amount_is_read() -> None:
    """Live 2026-11-03 (SHI-SS-261027-024): "OTHERS Fastag 600" read with the
    600 in the label and 0 as the amount: the FASTag was lost. A receipt's
    number in its label is never an amount."""
    from audit_core.uc03_account_statement import read_statement

    read = read_statement([{"particulars_raw": "OTHERS Fastag 600", "debit_amount": 0},
                           {"particulars_raw": "RECEIPT 0573", "credit_amount": 0},
                           {"particulars_raw": "RECEIPT MR 1573", "credit_amount": None}])
    assert read.components == {"fastag_amount": D(600)}
    assert read.receipts == 0 and read.unmapped == []


def test_the_checks_count_only_the_discounts_the_deal_counts() -> None:
    """Live 2026-11-03 (SHI-SS-261031-004): the invoice's Rs 30,000.60 the NDC
    does not give, set aside by the Deal, was raised as an excess discount."""
    from audit_core import uc03_discount_given as dg

    invoice_only = {"key": "CASH_DISCOUNT", "billed": "30000.60", "current": None, "flags": ["NOT_ON_STATEMENT"]}
    same_as = {"key": "EXCHANGE_BONUS", "billed": None, "current": None, "sameAs": {"key": "SCRAPPAGE_BONUS_DEALER"}}
    in_price = {"key": "CASH_DISCOUNT", "billed": "49999.60", "current": None, "inPrice": True}
    counted = {"key": "CORPORATE_PRIVILEGE", "billed": "30000", "current": "30000"}
    assert [dg.has_figure(r) for r in (invoice_only, same_as, in_price, counted)] == [False, False, False, True]
    assert dg.given(invoice_only) == (None, None) and dg.given(counted) == (D(30000), "billed")


def test_a_figure_within_ten_rupees_of_its_standard_is_not_flagged() -> None:
    """Live 2026-11-03 (SHI-SS-261027-007): Rs 9,99,901 against the price
    master's Rs 9,99,901.32 was flagged "below standard"."""
    from audit_core.uc03_p2_journey360 import _flags, _measured_flags

    assert _flags("COMMERCIAL", "999901.32", None, "999901", None) == []
    assert _flags("COMMERCIAL", "999901.32", None, "999881", None) == ["BELOW_STANDARD"]
    assert _measured_flags("COMMERCIAL", [], "999901", "999901.32") == []


def test_a_bh_series_rto_is_not_compared_with_the_price_master(journey) -> None:
    """Product owner 2026-11-03: "Don't compare BH RTO". The price master's
    RTO figure is a state registration's; SHI-SS-261027-007's BH-series RTO
    of Rs 18,500 was held Rs 62,232 "undercharged" against Rs 80,732. The RTO
    challan's two-year road tax tells a BH registration."""
    from audit_core.uc03_p2_journey360 import _bh_series, _rto_without_standard

    def bh() -> bool:
        with journey.engine.begin() as c:
            set_tenant_context(c, journey.tenant_id)
            return _bh_series(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)

    add_ready_document(journey, "rto_challan", process_area="DELIVERY", line_items=[
        {"description_raw": "MV Tax(14-Oct-2026 to One Time)", "amount": 130000}])
    assert bh() is False
    add_ready_document(journey, "rto_challan", process_area="DELIVERY", line_items=[
        {"description_raw": "MV Tax(14-Oct-2026 to 13-Oct-2028)", "amount": 11299}])
    assert bh() is True

    rows = [{"key": "road_tax_registration", "standard": "80732.11", "variance": "-62232.11",
             "flags": ["BELOW_STANDARD", "INVOICES_DISAGREE"]},
            {"key": "insurance_amount", "standard": "41415", "variance": "-2034", "flags": ["BELOW_STANDARD"]}]
    _rto_without_standard(rows)
    assert (rows[0]["standard"], rows[0]["variance"], rows[0]["flags"]) == (None, None, ["INVOICES_DISAGREE"])
    assert rows[1]["standard"] == "41415"


def test_a_financed_cars_rto_standard_includes_the_hypothecation_fee(journey) -> None:
    """Product owner 2026-11-04: 74 journeys were Rs 1,500 above the price
    master's RTO, each a financed car -- the RTO's hypothecation fee."""
    from audit_core.uc03_p2_journey360 import _financed, _hypothecation_on_rto

    rows = [{"key": "road_tax_registration", "standard": "80732.11", "flags": []},
            {"key": "registration_charges", "standard": None, "partOf": "road_tax_registration", "flags": []},
            {"key": "insurance_amount", "standard": "41415", "flags": []}]
    _hypothecation_on_rto(rows)
    assert rows[0]["standard"] == "82232.11" and "hypothecation" in rows[0]["standardBasis"]
    assert rows[1]["standard"] is None and rows[2]["standard"] == "41415"
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        assert _financed(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id) is False


def test_a_tcs_in_lakhs_on_the_booking_docket_is_a_misread_and_not_shown() -> None:
    """Live 2026-11-04 (SHI-SS-261027-007): the booking form's vehicle price,
    Rs 9,99,901, was read as its TCS and shown. TCS is 1% of the ex-showroom."""
    from audit_core.uc03_p2_journey360 import _tcs_read_wrongly

    rows = [{"key": "ex_showroom_price", "current": "999901", "billed": "999901", "booking": None,
             "standard": "999901.32", "flags": []},
            {"key": "tcs_amount", "current": "0", "billed": None, "booking": "999901", "standard": "0", "flags": []}]
    _tcs_read_wrongly(rows)
    assert rows[1]["booking"] is None and "READ_ERROR" in rows[1]["flags"]
    rows[1]["booking"], rows[1]["flags"] = "9999", []
    _tcs_read_wrongly(rows)
    assert rows[1]["booking"] == "9999" and rows[1]["flags"] == []
