"""The DMS printout's minus-signed credits are its convention, not a misreading
(2026-11-23). Pure: no database. Rows as read off ADI-RD-261101-051's printout."""
from __future__ import annotations

import json

from audit_core.uc03_p2_stage import statement_out_of_balance

PRINTOUT = [
    {"particulars_raw": "Ex-Showroom", "debit_amount": 1030000},
    {"particulars_raw": "Registration Charges", "debit_amount": 103739},
    {"particulars_raw": "TCS", "debit_amount": 10300},
    {"particulars_raw": "Other Discount", "credit_amount": -3000},
    {"particulars_raw": "2026-09-30, Journal_NonGST, 24800, RABINDRA KUMAR DANDAPATI-APHP D7920G",
     "credit_amount": -1189789},
    {"particulars_raw": "2026-09-30, Journal_NonGST, 24831, Mahindra & Mahindra Ltd( RSA) Payable", "debit_amount": 2021},
    {"particulars_raw": "2026-09-30, Journal_NonGST, 24832, GENERALI CENTRAL INSURANCE COMPANY LIMITED-AM CTC",
     "debit_amount": 40148},
]


def test_a_printout_with_minus_credits_adds_up():
    assert statement_out_of_balance({"line_items": json.dumps(PRINTOUT), "balance_amount": "-6581"}) is None


def test_a_printout_misread_still_does_not_add_up():
    rows = [dict(r) for r in PRINTOUT]
    rows[2]["debit_amount"] = 103000
    assert statement_out_of_balance({"line_items": json.dumps(rows), "balance_amount": "-6581"}) is not None


def test_an_ndc_form_with_a_negative_total_is_still_a_misreading():
    ndc = [
        {"particulars_raw": "EX-SHOW ROOM PRICE OF VEHICLE", "debit_amount": 1030000},
        {"particulars_raw": "BOOKING AMOUNT", "credit_amount": -1030000},
    ]
    assert statement_out_of_balance({"line_items": json.dumps(ndc), "balance_amount": "0"}) is not None
