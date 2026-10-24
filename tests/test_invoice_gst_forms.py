"""An invoice prints GST in one of two forms -- separately (line items / header
amounts) or inside one combined total. Either way the invoice's own figures
give the GST-inclusive vehicle price; a missing printed rate must not drop it
(Deal showed -3.95 lakh against standard: discounts kept, price dropped)."""
from __future__ import annotations

from decimal import Decimal

from audit_core.uc03_invoice_materialization import read_invoice

_VEHICLE = {"invoice_purpose": "VEHICLE_SALE"}


def _read(raw: dict) -> dict:
    return read_invoice("tax_invoice_tally", {**_VEHICLE, **raw})


def test_combined_gst_printed_price_is_already_inclusive():
    out = _read({"gross_amount_before_discount": "1000000", "invoice_discount_amount": "50000",
                 "tcs_amount": "9500", "grand_total_amount": "959500"})
    assert out["lines"]["ex_showroom_price"] == Decimal("1000000.00")
    assert out["discounts"]["UNCLASSIFIED_DISCOUNT"]["amount"] == Decimal("50000.00")
    assert out["vehicle"]["saleValue"] == Decimal("950000.00")
    assert out["vehicle"]["taxesRead"] is True and out["vehicle"]["gstBasis"] == "INCLUDED_IN_PRICE"
    assert out["issues"] == []


def test_gst_in_the_total_only_is_worked_out_from_the_total():
    # 10,50,000 - 50,000 = 10,00,000 taxable; the total 12,80,000 + TCS 10,000.
    out = _read({"gross_amount_before_discount": "1050000", "invoice_discount_amount": "50000",
                 "tcs_amount": "10000", "grand_total_amount": "1290000"})
    assert out["lines"]["ex_showroom_price"] == Decimal("1344000.00")
    assert out["discounts"]["UNCLASSIFIED_DISCOUNT"]["amount"] == Decimal("64000.00")
    # price and discounts are on the same basis: sale value is the total less TCS
    assert out["vehicle"]["saleValue"] == Decimal("1280000.00")
    assert out["vehicle"]["gstBasis"] == "GRAND_TOTAL"


def test_separate_gst_line_items_give_the_total():
    out = _read({"gross_amount_before_discount": "1050000", "invoice_discount_amount": "50000",
                 "line_items": [{"line_category": "TAX_LINE", "description_raw": "CGST", "net_amount": "140000"},
                                {"line_category": "TAX_LINE", "description_raw": "SGST", "net_amount": "140000"}]})
    assert out["lines"]["ex_showroom_price"] == Decimal("1344000.00")
    assert out["discounts"]["UNCLASSIFIED_DISCOUNT"]["amount"] == Decimal("64000.00")
    assert out["vehicle"]["gstBasis"] == "TAX_LINES"


def test_no_price_printed_and_no_discount_the_total_is_the_price():
    out = _read({"tcs_amount": "10000", "grand_total_amount": "1290000"})
    assert out["lines"]["ex_showroom_price"] == Decimal(1280000)
    assert out["vehicle"]["taxesRead"] is True


def test_a_printed_rate_is_used_as_printed():
    out = _read({"gross_amount_before_discount": "1050000", "invoice_discount_amount": "50000",
                 "taxable_amount": "1000000", "cgst_rate": "14", "sgst_rate": "14",
                 "grand_total_amount": "1280000"})
    assert out["lines"]["ex_showroom_price"] == Decimal("1344000.00")
    assert out["vehicle"]["gstBasis"] == "PRINTED_RATE"


def test_nothing_to_work_it_out_from_stays_unread():
    out = _read({"taxable_amount": "1600000"})
    assert "ex_showroom_price" not in out["lines"]
    assert [i["code"] for i in out["issues"]] == ["TAXES_NOT_READ"]


def test_a_total_that_cannot_be_gst_is_not_used():
    # total below the price: not GST, not a reading we can trust
    out = _read({"gross_amount_before_discount": "1000000", "grand_total_amount": "400000"})
    assert "ex_showroom_price" not in out["lines"]
    # more than 50% over the price: not GST either
    out = _read({"gross_amount_before_discount": "1000000", "grand_total_amount": "1700000"})
    assert "ex_showroom_price" not in out["lines"]


def test_add_ons_sharing_the_total_are_never_guessed_around():
    out = _read({"grand_total_amount": "1400000",
                 "line_items": [{"line_category": "VEHICLE", "gross_amount": "1000000"},
                                {"line_category": "ACCESSORY_GENUINE", "net_amount": "25000"}]})
    assert "ex_showroom_price" not in out["lines"]


# ── a printed rate the invoice's own total contradicts (live data 2026-10-24) ──
def _printed(gross, discount, rate_each, total, tcs=None, **extra):
    """CGST and SGST each printed at ``rate_each`` per cent (the reading doubled
    the rate: 18% printed as 18 + 18)."""
    raw = {"gross_amount_before_discount": gross, "invoice_discount_amount": discount,
           "cgst_rate": rate_each, "sgst_rate": rate_each, "grand_total_amount": total, **extra}
    if tcs:
        raw["tcs_amount"] = tcs
    return _read(raw)


def test_a_doubled_rate_is_corrected_by_the_total():
    # 8,55,084.75 - 2,288 = 8,52,796.75 taxable; 18% -> 10,06,300.17; + TCS 10,063
    out = _printed("855084.75", "2288", "18", "1016363.17", tcs="10063")
    assert out["vehicle"]["gstBasis"] == "RATE_FROM_TOTAL" and out["vehicle"]["gstRate"] == Decimal("0.1800")
    assert out["lines"]["ex_showroom_price"] == Decimal("1009000.01")
    assert out["vehicle"]["saleValue"] == Decimal("1006300.17")


def test_eighty_per_cent_is_forty_and_ten_per_cent_is_five():
    out = _printed("1283571.44", "3571.42", "40", "1809920.03", tcs="17920")
    assert out["vehicle"]["gstRate"] == Decimal("0.4000")
    assert out["lines"]["ex_showroom_price"] == Decimal("1797000.02")
    out = _printed("2923809.52", "0", "5", "3100700", tcs="30700")
    assert out["vehicle"]["gstRate"] == Decimal("0.0500")


def test_a_wrong_rate_that_is_not_a_half_is_corrected_too():
    out = _read({"gross_amount_before_discount": "815254.24", "cgst_rate": "14", "sgst_rate": "14",
                 "grand_total_amount": "962000"})
    assert out["vehicle"]["gstRate"] == Decimal("0.1800")
    assert out["lines"]["ex_showroom_price"] == Decimal("962000.00")


def test_a_rate_that_reproduces_the_total_is_left_alone():
    out = _printed("857203.43", "21186.20", "9", "986500.33")
    assert out["vehicle"]["gstBasis"] == "PRINTED_RATE" and out["vehicle"]["gstRate"] == Decimal("0.18")
    assert out["lines"]["ex_showroom_price"] == Decimal("1011500.05")


def test_a_total_that_no_rate_explains_leaves_the_printed_rate():
    # a discount not read: price after discount x 1.18 is Rs 37,000 over the total
    out = _read({"gross_amount_before_discount": "802966.10", "cgst_rate": "9", "sgst_rate": "9",
                 "grand_total_amount": "910499.68"})
    assert out["vehicle"]["gstBasis"] == "PRINTED_RATE"
    assert out["lines"]["ex_showroom_price"] == Decimal("947500.00")


def test_add_ons_sharing_the_total_never_correct_a_printed_rate():
    out = _read({"cgst_rate": "9", "sgst_rate": "9", "grand_total_amount": "1400000",
                 "line_items": [{"line_category": "VEHICLE", "gross_amount": "1000000", "taxable_amount": "1000000"},
                                {"line_category": "ACCESSORY_GENUINE", "net_amount": "25000"}]})
    assert out["vehicle"]["gstBasis"] == "PRINTED_RATE"
