from __future__ import annotations

from audit_core import uc03_model_attribute_matching as m

# Real alias rows, copied from migration 0061's seeded ``oem_model_aliases``.
_MAHINDRA_ALIASES = [
    ("SCORPIO", "SCORPIO CLASSIC"),
    ("SCORPIO CLASSIC", "SCORPIO CLASSIC"),
    ("SCORPIO N", "SCORPIO N"),
    ("SCORPIO-N", "SCORPIO N"),
    ("NEW SCORPIO N", "NEW SCORPIO N"),
    ("XUV 7XO", "XUV 7XO"),
    ("XUV700", "XUV 7XO"),
    ("XUV7XO", "XUV 7XO"),
]


def _row(model, variant, *, fuel=None, transmission=None, drive=None, seater=None, sku="SKU1"):
    return {
        "product_sku_id": sku,
        "sku_code": sku,
        "model_name": model,
        "variant_name": variant,
        "colour_name": None,
        "fuel_powertrain": fuel,
        "transmission": transmission,
        "drive": drive,
        "seater": seater,
    }


# ── resolve_model_via_aliases ────────────────────────────────────────────────
def test_resolves_scorpio_n_not_the_shorter_scorpio_alias() -> None:
    resolved = m.resolve_model_via_aliases(
        model_name="SCORPIO N Z8 (S)", oem_aliases=_MAHINDRA_ALIASES
    )
    assert resolved is not None
    canonical, remainder = resolved
    assert canonical == "SCORPIO N"
    assert remainder.replace(" ", "") == "Z8S"


def test_does_not_resolve_new_scorpio_n_when_text_lacks_new_prefix() -> None:
    canonical, _ = m.resolve_model_via_aliases(
        model_name="SCORPIO N Z8 (S)", oem_aliases=_MAHINDRA_ALIASES
    )
    assert canonical != "NEW SCORPIO N"


def test_resolves_new_scorpio_n_when_text_has_new_prefix() -> None:
    canonical, remainder = m.resolve_model_via_aliases(
        model_name="NEW SCORPIO N Z8 S", oem_aliases=_MAHINDRA_ALIASES
    )
    assert canonical == "NEW SCORPIO N"
    assert remainder.replace(" ", "") == "Z8S"


def test_resolves_hyphenated_xuv_700_against_glued_alias() -> None:
    canonical, remainder = m.resolve_model_via_aliases(
        model_name="XUV-7XO", oem_aliases=_MAHINDRA_ALIASES
    )
    assert canonical == "XUV 7XO"
    assert remainder == ""


def test_no_alias_prefix_returns_none() -> None:
    assert m.resolve_model_via_aliases(model_name="THAR ROXX", oem_aliases=_MAHINDRA_ALIASES) is None


# ── match_by_attributes ──────────────────────────────────────────────────────
def test_scorpio_n_z8s_matches_the_one_diesel_at_2wd_7str_variant() -> None:
    # Real ingested master rows for SCORPIO N (non-NEW).
    rows = [
        _row("SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7", sku="Z8S"),
        _row("SCORPIO N", "Z8T D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7", sku="Z8T"),
        _row("SCORPIO N", "Z8 L D AT 2WD 6 STR BS6.2 - N - ADAS", fuel="DIESEL", transmission="AT", drive="2WD", seater="6", sku="Z8L6"),
        _row("SCORPIO N", "Z8 S G AT 2WD 7 STR BS6.2 - N", fuel="PETROL", transmission="AT", drive="2WD", seater="7", sku="Z8SPETROL"),
    ]
    matched = m.match_by_attributes(
        rows, oem_code="MAHINDRA", model_remainder="Z8 (S)", variant_text="DAT 2WD 7STR"
    )
    assert [r["sku_code"] for r in matched] == ["Z8S"]


def test_xuv7xo_ax7l_diesel_matches_the_one_2wd_variant_not_the_awd_one() -> None:
    rows = [
        _row("XUV 7XO", "AX7L DSL AT 7 STR", fuel="DIESEL", transmission="AT", drive="2WD", seater="7", sku="AX7L_2WD"),
        _row("XUV 7XO", "AX7L DSL AT AWD 7 STR", fuel="DIESEL", transmission="AT", drive="AWD", seater="7", sku="AX7L_AWD"),
        _row("XUV 7XO", "AX7T DSL AT 7 STR", fuel="DIESEL", transmission="AT", drive="2WD", seater="7", sku="AX7T"),
    ]
    matched = m.match_by_attributes(
        rows, oem_code="MAHINDRA", model_remainder="", variant_text="AX-7L(D) AT 2WD 7STR"
    )
    assert [r["sku_code"] for r in matched] == ["AX7L_2WD"]


def test_ambiguous_when_booking_form_gives_no_disambiguating_signal() -> None:
    # Dealer wrote only the trim, no fuel/transmission/seater -- both
    # candidates remain plausible, so this must NOT silently pick one.
    rows = [
        _row("SCORPIO N", "Z8T D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7", sku="AT"),
        _row("SCORPIO N", "Z8T D MT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="MT", drive="2WD", seater="7", sku="MT"),
    ]
    matched = m.match_by_attributes(
        rows, oem_code="MAHINDRA", model_remainder="Z8T", variant_text=None
    )
    assert {r["sku_code"] for r in matched} == {"AT", "MT"}


def test_unknown_oem_is_a_no_op() -> None:
    rows = [_row("SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7")]
    matched = m.match_by_attributes(
        rows, oem_code="HYUNDAI", model_remainder="Z8 (S)", variant_text="DAT 2WD 7STR"
    )
    assert matched == []
