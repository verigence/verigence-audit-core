"""A shortlist is settled by the variant every word of which the document
prints (2026-10-20: "BOL MAXX PUP HD 1.7 LX" left 11 Maxx HD rows)."""
from __future__ import annotations

from audit_core.uc03_model_resolution import _variant_words_pick


def _row(variant, model="MAXX HD"):
    return {"model_name": model, "variant_name": variant, "sku_code": f"{model}::{variant}"}


MAXX_HD = [_row(v) for v in (
    "MAXX HD 1.7 LX BS6.2", "MAXX HD 1.7L LX BS6.2", "MAXX HD 1.7L MXi BS6.2", "MAXX HD 1.7 LX CBC BS6.2",
    "MAXX HD 1.3 LX BS6.2", "MAXX HD 1.7L SXi AC - BS6.2", "MAXX HD 1.7 MXi BS6.2", "MAXX HD 1.7 VXi AC - BS6.2",
)]


def test_the_invoice_wording_names_one_maxx_hd():
    picked = _variant_words_pick(MAXX_HD, {"model_name": "BOL MAXX PUP HD 1.7 LX", "variant_name": "HD 1.7 LX"})
    assert picked["variant_name"] == "MAXX HD 1.7 LX BS6.2"
    picked = _variant_words_pick(MAXX_HD, {"model_name": "BOL MAXX PUP HD 1.7L LX", "variant_name": "LX"})
    assert picked["variant_name"] == "MAXX HD 1.7L LX BS6.2"


def test_private_and_commercial_lines_of_one_variant_stay_with_the_pc():
    rows = [_row("N11 BS6.2 - Refresh (Private)", "THE BOSS- BOLERO+NEO"),
            _row("N11 BS6.2 - Refresh (Commercial)", "THE BOSS- BOLERO+NEO")]
    assert _variant_words_pick(rows, {"model_name": "BOLERO NEO", "variant_name": "N11 PW+SB"}) is None


def test_a_model_only_reading_pins_nothing():
    assert _variant_words_pick(MAXX_HD, {"model_name": "MAXX", "variant_name": "HD"}) is None
    rows = [_row("AX7 DMT 2WD - Star Edition", "THAR ROXX"), _row("AX7L DMT 2WD", "THAR ROXX")]
    assert _variant_words_pick(rows, {"model_name": "THAR ROXX", "variant_name": "AX7 L D MT"}) is None
