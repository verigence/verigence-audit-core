"""Word identification of a SKU (2026-10-20 product owner rules R3 / R4).

The rows below carry the real Mahindra price-list variant texts (01/03 Sep
and Oct 2026 consolidated lists, EV lists) and the wordings real booking
forms and invoices print. Price is never an input here except to order a
shortlist and to check an already word-identified SKU."""
from __future__ import annotations

from decimal import Decimal

import pytest

from audit_core.uc03_model_resolution import invoice_ex_showroom
from audit_core.uc03_sku_words import identify, order_shortlist, price_check, raw_tokens

ALIASES = [
    ("BOL MAXX PUP HD", "MAXX HD"), ("MAXX PUP HD", "MAXX HD"), ("BOLERO MAXX PUP HD", "MAXX HD"),
    ("BOLERO MAXX PUP CITY", "MAXX CITY"), ("MAXX PUP CITY", "MAXX CITY"),
    ("XUV 3XO", "XUV3XO"), ("XUV-7XO", "XUV 7XO"), ("SCORPIO-N", "SCORPIO N"), ("THAR", "NEW THAR 2WD & 4WD"),
    ("BOLERO NEO", "BOLERO NEO"), ("NEW SCORPIO N", "NEW SCORPIO N"),
]


def _row(model, variant, ex="1000000", *, fuel=None, transmission=None, drive=None, seater=None, code=None,
         category=None):
    return {
        "product_sku_id": code or f"{model}::{variant}", "sku_code": code or f"{model}::{variant}",
        "model_name": model, "variant_name": variant, "colour_name": None,
        "fuel_powertrain": fuel, "transmission": transmission, "drive": drive, "seater": seater,
        "category": category, "master_ex_showroom": Decimal(ex),
    }


MAXX_HD = [_row("MAXX HD", v, fuel="DIESEL") for v in (
    "MAXX HD 1.3 LX BS6.2", "MAXX HD 1.7 LX BS6.2", "MAXX HD 1.7 LX CBC BS6.2", "MAXX HD 1.7 MXi BS6.2",
    "MAXX HD 1.7 VXi AC - BS6.2", "MAXX HD 1.7L LX BS6.2", "MAXX HD 1.7L MXi BS6.2", "MAXX HD 1.7L SXi AC - BS6.2",
    "MAXX HD 2.0L SXi AC - BS6.2", "MAXX HD 2.0L SXi CBC AC - BS6.2", "MAXX HD 2.0L VXi AC - BS6.2",
)]
MAXX_CITY = [_row("MAXX CITY", v, fuel="DIESEL") for v in (
    "MAXX CITY 1.3 LX BS6.2", "MAXX CITY 1.3 VXi AC - BS6.2", "MAXX CITY 1.4 LX BS6.2", "MAXX CITY 1.5 LX BS6.2",
    "MAXX CITY 1.5 LX CBC BS6.2", "MAXX CITY 1.5 MXi BS6.2", "MAXX CITY 1.5 VXi AC - BS6.2",
)]
XUV3XO = [
    _row("XUV3XO", v, fuel=f, transmission=t) for v, f, t in (
        ("AX5 DS AS", "DIESEL", "AT"), ("AX5 DS MT", "DIESEL", "MT"), ("AX5 PM AT", "PETROL", "AT"),
        ("AX5 PM MT", "PETROL", "MT"), ("AX5L TGDI AT", "PETROL", "AT"), ("AX5L TGDI MT", "PETROL", "MT"),
        ("MX3 DS AS", "DIESEL", "AT"), ("MX3 DS MT", "DIESEL", "MT"), ("MX3 PM AT", "PETROL", "AT"),
        ("MX3 PRO PM AT", "PETROL", "AT"), ("MX2 DS MT", "DIESEL", "MT"), ("MX2 PRO DS MT", "DIESEL", "MT"),
    )
] + [
    _row("XUV3XO EV", "XUV3XO EV - AX5 FH", "1389000", fuel="ELECTRIC", category="BEV"),
    _row("XUV3XO EV", "XUV3XO EV - AX7 L FH", "1496000", fuel="ELECTRIC", category="BEV"),
]
XUV7XO = [_row("XUV 7XO", v, fuel="DIESEL", transmission="AT", drive=d, seater=s) for v, d, s in (
    ("AX7 DSL AT 7 STR", "2WD", "7"), ("AX7L DSL AT 7 STR", "2WD", "7"), ("AX7L DSL AT 7 STR DT", "2WD", "7"),
    ("AX7L DSL AT 6 STR", "2WD", "6"), ("AX7L DSL AT AWD 7 STR", "AWD", "7"),
)]
SCORPIO = [_row("SCORPIO N", v) for v in (
    "Z8 S D AT 2WD 7 STR BS6.2 - N", "Z8 S G AT 2WD 7 STR BS6.2 - N", "Z8T D AT 2WD 7 STR BS6.2 - N",
    "Z8 L D AT 4WD 7 STR BS6.2 - N - ADAS",
)]
BE6 = [_row("BE 6", f"BE 6 Two B59 R19 {c}", fuel="ELECTRIC") for c in ("C11", "C7", "NCH")] + [
    _row("BE6 SPORTEQ", "BE 6 SPORTEQ THREE B79 R19 C11", fuel="ELECTRIC"),
    _row("BE6 SPORTEQ", "BE 6 SPORTEQ THREE+ B79 R19 C11", fuel="ELECTRIC"),
]
NEO = [
    _row("BOLERO NEO", "N10 BS6.2 - R - Refresh (Private)", code="NEO::N10R::PRI"),
    _row("BOLERO NEO", "N10 BS6.2 - R - Refresh (Commercial)", code="NEO::N10R::COM"),
    _row("BOLERO NEO", "N10 OPT BS6.2 - Refresh (Private)", code="NEO::N10OPT::PRI"),
]
ROWS = MAXX_HD + MAXX_CITY + XUV3XO + XUV7XO + SCORPIO + BE6 + NEO


def _id(model, variant=None, **kwargs):
    return identify(rows=ROWS, model_text=model, variant_text=variant, aliases=ALIASES, **kwargs)


@pytest.mark.parametrize(("model", "variant", "expected"), [
    # Spellings the documents print -> the row every distinguishing word names.
    ("BOL MAXX PUP HD 1.7 LX", None, "MAXX HD 1.7 LX BS6.2"),
    ("BOLERO MAXX PUP HD 1.7L LX", None, "MAXX HD 1.7L LX BS6.2"),
    ("MAXX PUP HD 1.7 L LX", None, "MAXX HD 1.7L LX BS6.2"),  # "1.7 L" is 1.7L, never 1.7
    ("MAXX PUP HD 1.7 LX CBC", None, "MAXX HD 1.7 LX CBC BS6.2"),
    ("BOLERO MAXX PUP HD 2.0L VXI", None, "MAXX HD 2.0L VXi AC - BS6.2"),  # no non-AC 2.0L VXi exists
    ("BOLERO MAXX PUP CITY 1.5 LXD", None, "MAXX CITY 1.5 LX BS6.2"),  # LXD = LX diesel
    ("Maxx", "HD 1.7 LX", "MAXX HD 1.7 LX BS6.2"),  # model split over the two boxes
    ("XUV 3XO", "AX5 L 1.2 TGDi MT", "AX5L TGDI MT"),
    ("XUV 3XO", "MX3 PRO PETROL AT", "MX3 PRO PM AT"),
    ("XUV 3XO", "MX3 PETROL AT", "MX3 PM AT"),  # never the PRO
    ("XUV 3XO", "MX2 PRO D MT", "MX2 PRO DS MT"),
    ("XUV 3XO", "MX3 DIESEL AMT", "MX3 DS AS"),  # AMT is the list's AS
    ("XUV 3XO EV", "AX5", "XUV3XO EV - AX5 FH"),
    ("XUV 3XO", "AX7 L EV", "XUV3XO EV - AX7 L FH"),
    ("XUV-7XO", "AX-7L(D) AT 7STR", "AX7L DSL AT 7 STR"),  # not AX7, not DT, not AWD, not 6 STR
    ("SCORPIO N Z8 (S)", "DAT 2WD 7STR", "Z8 S D AT 2WD 7 STR BS6.2 - N"),
    ("SCORPIO N", "Z8L DIESEL AT 4WD 7 STR", "Z8 L D AT 4WD 7 STR BS6.2 - N - ADAS"),
    ("BE 6", "TWO B59 R19 C11", "BE 6 Two B59 R19 C11"),
    ("BE6 SPORTEQ", "THREE+ B79 R19 C11", "BE 6 SPORTEQ THREE+ B79 R19 C11"),  # "BE 6" vs "BE6"; THREE+ != THREE
])
def test_the_words_name_exactly_one_row(model, variant, expected):
    result = _id(model, variant)
    assert result.status == "IDENTIFIED", (result.status, [r["variant_name"] for r in result.candidates])
    assert result.row["variant_name"] == expected


@pytest.mark.parametrize(("model", "variant", "status"), [
    ("XUV 3XO", "AX5", "CATEGORY_UNCERTAIN"),  # petrol, diesel or the EV: never the price
    ("XUV 3XO", "AX5 MT", "CATEGORY_UNCERTAIN"),
    ("XUV 3XO", None, "CATEGORY_UNCERTAIN"),
    ("XUV 3XO", "AX5 EV DIESEL", "CATEGORY_CONFLICT"),
    ("XUV 3XO", "AX5 PETROL", "AMBIGUOUS"),  # PM MT or PM AT
    ("BE 6", "PACK TWO 59 KWH", "AMBIGUOUS"),  # the charger (C11 / C7 / NCH) is not stated
    ("MAXX PUP HD", None, "AMBIGUOUS"),  # the model alone never names a row
    ("BOLERO NEO", "N10 R", "REGISTRATION_UNKNOWN"),
    ("XUV 3XO", "AX9 PETROL MT", "UNRECOGNISED_WORD"),  # a trim the list does not have
    ("XUV300", "W7L EV", "NO_MODEL"),
])
def test_anything_but_one_row_is_a_task(model, variant, status):
    result = _id(model, variant)
    assert result.status == status
    assert result.row is None


def test_the_registration_type_decides_private_or_commercial():
    assert _id("BOLERO NEO", "N10 R", registration="PRIVATE").row["sku_code"] == "NEO::N10R::PRI"
    assert _id("BOLERO NEO", "N10 R", registration="COMMERCIAL").row["sku_code"] == "NEO::N10R::COM"


def test_an_invoices_gst_rate_decides_ev_or_ice():
    assert _id("XUV 3XO", "AX5", gst_rate=Decimal(5)).row["variant_name"] == "XUV3XO EV - AX5 FH"
    assert _id("XUV 3XO", "AX5 PETROL MT", gst_rate=Decimal(18)).row["variant_name"] == "AX5 PM MT"
    assert _id("XUV 3XO", "AX5 DIESEL MT", gst_rate=Decimal(5)).status == "CATEGORY_CONFLICT"


def test_generation_siblings_with_the_same_words_are_left_to_the_pc():
    rows = [_row("SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - N"),
            _row("NEW SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - Refresh")]
    result = identify(rows=rows, model_text="SCORPIO N Z8 (S)", variant_text="DAT 2WD 7STR", aliases=ALIASES)
    assert result.status == "AMBIGUOUS" and len(result.candidates) == 2


def test_a_model_renamed_between_months_resolves_under_either_spelling():
    """Coordinator 2026-10-20: September's and October's spellings resolve
    on either book (XUV3XO / XUV 3XO, NEW THAR 2WD & 4WD / NEW THAR,
    New SCORPIO N / SCORPIO-N, SCORPIO CLASSIC / SCORPIO)."""
    october = [_row("XUV 3XO", "AX5L TGDI MT", fuel="PETROL", transmission="MT"),
               _row("NEW THAR", "LXT D AT 4WD 4S HT BS6.2 - Refresh"),
               _row("SCORPIO-N", "Z8 L D AT 4WD 7 STR BS6.2 - N"),
               _row("SCORPIO", "Classic S11 BS6.2 - E")]
    for model, variant, expected in (
        ("XUV3XO", "AX5L TGDI MT", "AX5L TGDI MT"),
        ("THAR", "LXT D AT 4WD", "LXT D AT 4WD 4S HT BS6.2 - Refresh"),
        ("NEW SCORPIO N", "Z8L D AT 4WD 7 STR", "Z8 L D AT 4WD 7 STR BS6.2 - N"),
        ("SCORPIO CLASSIC", "S11", "Classic S11 BS6.2 - E"),
    ):
        result = identify(rows=october, model_text=model, variant_text=variant, aliases=ALIASES)
        assert result.status == "IDENTIFIED" and result.row["variant_name"] == expected, (model, result.status)


def test_normalisation_table():
    assert raw_tokens("AX5 L 1.2 TGDi") == ["AX5L", "1.2", "TGDI"]
    assert raw_tokens("Z8 (S) DAT") == ["Z8S", "DAT"]
    assert raw_tokens("REVX EDGE(O) M (O)") == ["REVX", "EDGEO", "MO"]
    assert raw_tokens("MAXX HD 1.7 L LX BS6.2") == ["MAXX", "HD", "1.7L", "LX"]
    assert raw_tokens("1.5XXL SD V2") == ["1.5", "XXL", "SD", "V2"]
    assert raw_tokens("PACK TWO 59 kWh") == ["PACK", "TWO", "B59"]
    assert raw_tokens("AX-7L(D)") == ["AX7L", "D"]


def test_price_check_allows_paise_rounding_only():
    assert price_check(Decimal("839501.15"), Decimal(839500)) == "MATCH"
    assert price_check(Decimal("839501.15"), Decimal("839503.15")) == "MATCH"
    assert price_check(Decimal("839501.15"), Decimal(852000)) == "DIFFERS"
    assert price_check(Decimal("839501.15"), None) == "MISSING"


def test_price_only_orders_a_shortlist():
    rows = [r for r in MAXX_CITY if " LX " in r["variant_name"] + " "]
    rows[0]["master_ex_showroom"] = Decimal(700000)
    ordered = order_shortlist(rows, model_text="MAXX CITY", variant_text="LX",
                              offered_ex_showroom=Decimal(701000))
    assert ordered[0] is rows[0]


# ── rule R1: the invoice's ex-showroom ────────────────────────────────────────
def test_r1_selling_price_times_the_printed_gst_rate():
    """The product owner's example: selling 7,22,033.90 x 1.18 = 8,52,000.00;
    the discount (21,016.58 before GST) is not taken off the ex-showroom."""
    ex, basis = invoice_ex_showroom({
        "gross_amount_before_discount": "722033.90", "invoice_discount_amount": "21016.58",
        "taxable_amount": "701017.32", "cgst_rate": "9", "sgst_rate": "9",
        "cgst_amount": "63091.56", "sgst_amount": "63091.56",
    })
    assert ex == Decimal("852000.00")
    assert basis["method"] == "SELLING_TIMES_GST_RATE" and basis["gstRatePercent"] == Decimal(18)


def test_r1_no_discount_adds_the_printed_gst_as_printed():
    ex, basis = invoice_ex_showroom({"taxable_amount": "722033.90", "cgst_amount": "64983.05",
                                     "sgst_amount": "64983.05", "cgst_rate": "9", "sgst_rate": "9"})
    assert ex == Decimal("852000.00") and basis["method"] == "SELLING_PLUS_PRINTED_GST"


def test_r1_igst_and_a_rate_read_off_the_amounts():
    ex, _ = invoice_ex_showroom({"gross_amount_before_discount": "1322857.14", "invoice_discount_amount": "10000",
                                 "taxable_amount": "1312857.14", "igst_rate": "5"})
    assert ex == Decimal("1389000.00")
    ex, basis = invoice_ex_showroom({"gross_amount_before_discount": "722033.90",
                                     "invoice_discount_amount": "21016.58", "taxable_amount": "701017.32",
                                     "cgst_amount": "63091.56", "sgst_amount": "63091.56"})
    assert ex == Decimal("852000.00") and basis["rate"] == "FROM_PRINTED_AMOUNTS"


def test_r1_nothing_to_go_on():
    assert invoice_ex_showroom({"taxable_amount": "701017.32", "invoice_discount_amount": "21016.58"})[0] is None
    assert invoice_ex_showroom({})[0] is None
