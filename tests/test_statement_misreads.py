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


# UTK-SB-261030-009: the closing "/-" read as a "1" on the three discounts
SLASH = {
    "total_cost": "1154657",
    "total_receipts": "1154657",
    "line_items": [
        {"particulars_raw": "Vehicle cost", "debit_amount": 1029901},
        {"particulars_raw": "Insurance amount", "debit_amount": 34993},
        {"particulars_raw": "Registration", "debit_amount": 61634},
        {"particulars_raw": "Accessories", "debit_amount": 28129},
        {"particulars_raw": "Finance D/O MMFSL MR No. 08228", "credit_amount": 624491},
        {"particulars_raw": "MR No. 01287", "credit_amount": 21000},
        {"particulars_raw": "MR No. 08303", "credit_amount": 199000},
        {"particulars_raw": "MR No. 08310", "credit_amount": 100000},
        {"particulars_raw": "MR No. 08735", "credit_amount": 30000},
        {"particulars_raw": "MR No.", "credit_amount": 50000},
        {"particulars_raw": "MR No.", "credit_amount": 73166},
        {"particulars_raw": "Scheme Discount", "credit_amount": 30001},
        {"particulars_raw": "Exchange Loyality", "credit_amount": 20001},
        {"particulars_raw": "Other Discount (if any)", "credit_amount": 70001},
    ],
}


def test_slash_readings():
    from audit_core.uc03_statement_misreads import slash_readings
    assert slash_readings(Decimal(70001)) == [Decimal(7000), Decimal(70000)]
    assert slash_readings(Decimal(624491)) == []
    assert slash_readings(Decimal(73166)) == []


def test_slash_dash_read_as_one_put_back():
    assert statement_differences(SLASH)
    fields, repaired = repair_comma_misreads(SLASH)
    assert statement_differences(fields) == []
    # 30001 and 20001 are within the Rs 10 the totals allow; 70001 is not
    assert {r["row"]: r["amount"] for r in repaired} == {"Other Discount (if any)": "7000"}


# 2026-11-17: every comma of a figure, and the printed totals themselves.
def test_every_comma_of_a_figure_may_be_read_as_a_one():
    # "8,74,808" with a "1" for each comma; either "1" may also be a real digit,
    # so the one-comma readings are offered too and the totals decide
    assert comma_readings(Decimal(81741808)) == [Decimal(874808), Decimal(8174808), Decimal(8741808)]
    # two "1"s side by side never stand for the same comma
    assert comma_readings(Decimal(1211000)) == [Decimal(121000)]
    # a "1" that stands where no comma would is never removed
    assert comma_readings(Decimal(8714808)) == []


def test_a_figure_with_both_commas_misread_is_put_back():
    both = {**JASMITA, "line_items": [
        {**row, "credit_amount": 81741808} if row["particulars_raw"].startswith("Finance") else row
        for row in JASMITA["line_items"]]}
    fields, repaired = repair_comma_misreads(both)
    assert statement_differences(fields) == []
    assert {r["row"]: (r["read"], r["amount"]) for r in repaired}["Finance D/O Banchon"] == ("81741808", "874808")


def test_a_printed_total_misread_the_same_way_is_put_back():
    read_right, _ = repair_comma_misreads(JASMITA)
    misread_total = {**read_right, "total_cost": "11167328"}  # "11,67,328"
    assert [d["total"] for d in statement_differences(misread_total)] == ["Total Cost"]
    fields, repaired = repair_comma_misreads(misread_total)
    assert statement_differences(fields) == []
    assert fields["total_cost"] == "1167328" and fields["line_items"] == read_right["line_items"]
    assert repaired == [{"row": "Total Cost", "side": "printed_total", "read": "11167328", "amount": "1167328"}]


def test_a_printed_total_is_never_worked_out_from_the_rows():
    read_right, _ = repair_comma_misreads(JASMITA)
    # a 6 read as a 3: no comma stands there, so nothing says which figure is wrong
    wrong_digit = {**read_right, "total_cost": "1137328"}
    fields, repaired = repair_comma_misreads(wrong_digit)
    assert repaired == [] and fields is wrong_digit
    # a total whose "1" removed does not give the rows' sum is left as read too
    other = {**read_right, "total_cost": "11267328"}
    fields, repaired = repair_comma_misreads(other)
    assert repaired == [] and fields is other


def test_the_search_is_bounded(monkeypatch):
    import audit_core.uc03_statement_misreads as misreads

    monkeypatch.setattr(misreads, "_MAX_TRIALS", 2)
    fields, repaired = repair_comma_misreads(JASMITA)  # needs three rows put back
    assert repaired == [] and fields is JASMITA
