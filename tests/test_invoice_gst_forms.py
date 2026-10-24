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
