from decimal import Decimal

from audit_core.uc03_statement_misreads import comma_readings, repair_comma_misreads
from audit_core.uc03_statement_totals import statement_differences

# UTK-SB-261114-012 (JASMITA PANI), the Utkal "Final Vehicle Accounts Statement" as read
JASMITA = {
    "statement_title": "FINAL VEHICLE ACCOUNTS STATEMENT",
    "total_cost": "1167328",
    "total_receipts": "1167308",
    "line_items": [
        {"particulars_raw": "Vehicle cost", "debit_amount": 999907},
        {"particulars_raw": "Insurance amount", "debit_amount": 37196},
        {"particulars_raw": "Registration", "debit_amount": 82233},
        {"particulars_raw": "Accessories", "debit_amount": 47492},
        {"particulars_raw": "Fastag charges", "debit_amount": 500},
        {"particulars_raw": "Finance D/O Banchon", "entry_date": "2026-10-14", "credit_amount": 8174808},
        {"particulars_raw": "MR No. 1435", "voucher_number": "1435", "entry_date": "2026-10-14", "credit_amount": 2120000},
        {"particulars_raw": "MR No. 8655", "voucher_number": "8655", "entry_date": "2026-10-07", "credit_amount": 21000},
        {"particulars_raw": "MR No. 1545", "voucher_number": "1545", "entry_date": "2026-11-02", "credit_amount": 44500},
        {"particulars_raw": "Other Discount (if any) Special", "credit_amount": 71000},
    ],
}


def test_comma_readings():
    assert comma_readings(Decimal(71000)) == [Decimal(7000)]
    assert comma_readings(Decimal(8174808)) == [Decimal(874808)]
    assert Decimal(220000) in comma_readings(Decimal(2120000))
    assert comma_readings(Decimal(21000)) == [Decimal(2000)]   # "2,000" -- the totals decide
    assert comma_readings(Decimal(12000)) == []
    assert comma_readings(Decimal(7000)) == []
    assert comma_readings(Decimal("71000.50")) == []


def test_jasmita_pani_ndc_put_back():
    assert statement_differences(JASMITA)
    fields, repaired = repair_comma_misreads(JASMITA)
    assert statement_differences(fields) == []
    amounts = {r["row"]: r["amount"] for r in repaired}
    assert amounts == {"Finance D/O Banchon": "874808", "MR No. 1435": "220000",
                       "Other Discount (if any) Special": "7000"}
    discount = next(r for r in fields["line_items"] if r["particulars_raw"].startswith("Other Discount"))
    assert discount["credit_amount"] == 7000


def test_copy_that_adds_up_is_left_alone():
    fields, _ = repair_comma_misreads(JASMITA)
    again, repaired = repair_comma_misreads(fields)
    assert repaired == [] and again is fields


def test_no_repair_when_it_does_not_add_up():
    wrong = {**JASMITA, "total_receipts": "1167000"}
    fields, repaired = repair_comma_misreads(wrong)
    assert repaired == [] and fields is wrong
