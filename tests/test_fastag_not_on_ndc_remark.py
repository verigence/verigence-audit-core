"""FASTag not on the NDC: a system remark (product owner 2026-11-18: "for fast tag
there is no tally invoice... if it is not in NDC then put a comment in system remarks")."""
from __future__ import annotations

from decimal import Decimal

from audit_core.uc03_account_statement import ledger_postings
from audit_core.uc03_system_remarks import fastag_not_on_ndc

_TALLY = {"fields": {"line_items": [
    {"entry_date": "2026-10-28", "particulars_raw": "Vehicle Sale 18%", "voucher_type": "Vehicle Sale",
     "voucher_number": "INV27C000196", "debit_amount": 1412990, "running_balance": "14,12,990.00 Dr"},
    {"entry_date": "2026-10-29", "particulars_raw": "ICICI BANK (788005000453)", "voucher_type": "Receipt",
     "voucher_number": "PB-26-27/0931", "credit_amount": 1555731, "running_balance": "1,42,741.00 Cr"},
    {"entry_date": "2026-10-29", "particulars_raw": "FAST TAG (SALES)", "voucher_type": "Sales_Inv",
     "voucher_number": "FT/PB/0089", "debit_amount": 600, "running_balance": "1.00 Cr"},
    {"entry_date": "2026-10-29", "particulars_raw": "Mahindra & Mahindra Ltd( RSA) Payable",
     "voucher_type": "Journal_NonGST", "voucher_number": "03958", "debit_amount": 2021},
]}}


def test_the_ledgers_fastag_and_rsa_postings_are_read() -> None:
    assert ledger_postings([_TALLY]) == {"fastag": Decimal(600), "rsa": Decimal(2021)}


def test_a_fastag_the_ndc_does_not_charge_is_a_remark_naming_the_ledgers_posting() -> None:
    remark = fastag_not_on_ndc({"ex_showroom_price": Decimal(1399000)}, {"fastag": Decimal(600)})
    assert remark["code"] == "FASTAG_NOT_ON_NDC" and "₹600" in remark["text"] and "not counted" in remark["text"]


def test_a_fastag_neither_charged_nor_posted_is_a_remark_that_it_was_not_taken() -> None:
    remark = fastag_not_on_ndc({"ex_showroom_price": Decimal(1399000)}, {})
    assert "did not take it" in remark["text"]


def test_a_fastag_on_the_ndc_or_no_ndc_read_is_no_remark() -> None:
    assert fastag_not_on_ndc({"fastag_amount": Decimal(600)}, {"fastag": Decimal(600)}) is None
    assert fastag_not_on_ndc(None, {"fastag": Decimal(600)}) is None
