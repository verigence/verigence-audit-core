"""The DMS printout filed as the NDC (product owner 2026-11-22): recognised by
its postings, never the NDC while an NDC is on file, and filling only the
charges the NDC does not give. Pure: no database. Rows as read off live Aditya
bookings."""
from __future__ import annotations

from decimal import Decimal

from audit_core.uc03_account_statement import combine_statements, read_statement

NDC = [
    {"particulars_raw": "EX-SHOW ROOM PRICE OF VEHICLE", "debit_amount": 1121500},
    {"particulars_raw": "LESS: CONSUMER OFFER (IF APPLICABLE)", "credit_amount": 22000},
    {"particulars_raw": "REGISTRATION COST", "debit_amount": 11640},
    {"particulars_raw": "BOOKING AMOUNT", "credit_amount": 21000},
    {"particulars_raw": "FINANCER AMOUNT", "credit_amount": 900000},
]
# ADI-RD-261112-018 / 261114-040's printout: whole-rupee ex-showroom, TCS the NDC lacks,
# its "Scheme Discount" before GST, and its postings.
PRINTOUT = [
    {"particulars_raw": "Ex-Showroom", "debit_amount": 1121500},
    {"particulars_raw": "Registration Charges", "debit_amount": 1640},
    {"particulars_raw": "TCS", "debit_amount": 11215},
    {"particulars_raw": "Scheme Discount", "credit_amount": 18644.07},
    {"particulars_raw": "Bank Finance DO (BDO), 02599, Axis Bank Ltd (Financer)", "credit_amount": 900000},
    {"particulars_raw": "Money Receipt (Bank), AMC-B/32559/26-27, PHONE PAY- AMC", "credit_amount": 65000},
    {"particulars_raw": "Old Car Value", "credit_amount": 210000},
]


def _doc(i, newer=False):
    return {"documentId": f"d{i}", "evidenceId": None, "fields": {}, "documentDate": "2026-11-20" if newer else None}


def test_the_ndc_stands_and_the_printout_fills_only_the_charges_it_lacks():
    read, combined = combine_statements([(_doc(1), NDC), (_doc(2, newer=True), PRINTOUT)])
    assert combined.primary["documentId"] == "d1"
    # the NDC's own figures stand (registration Rs 11,640, not the printout's 1,640)
    assert read.components == {"ex_showroom_price": Decimal(1121500), "registration_charges": Decimal(11640),
                               "tcs_amount": Decimal(11215)}
    assert combined.origin[("COMMERCIAL", "tcs_amount")]["documentId"] == "d2"
    # its discount before GST is never taken; the NDC's consumer offer stands
    assert read.discounts == {"CASH_DISCOUNT": Decimal(22000)}
    # nothing of the printout is compared with the NDC, nor listed as the NDC's rows
    assert combined.differences == []
    assert not any("OLD CAR VALUE" == u["label"] for u in read.unmapped)


def test_a_printout_alone_stands_as_before():
    read, combined = combine_statements([(_doc(2), PRINTOUT)])
    assert combined.primary["documentId"] == "d2"
    assert read.components["tcs_amount"] == Decimal(11215)


def test_an_ndc_without_the_printout_is_read_exactly_as_before():
    alone = read_statement(NDC)
    read, _ = combine_statements([(_doc(1), NDC)])
    assert (read.components, read.discounts) == (alone.components, alone.discounts)
