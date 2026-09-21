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


def _row(model, variant, *, fuel=None, transmission=None, drive=None, seater=None, trim=None, sku="SKU1"):
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
        "trim": trim,
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


def test_master_trim_field_takes_priority_over_variant_name_residue() -> None:
    """oem_price_masters.py now persists the masters sheet's own Trim
    column (product_variants.attributes->>'trim') -- this must be used
    directly instead of re-deriving it from variant_name, which can be
    polluted by words the vocabulary doesn't recognize (e.g. a leading
    "NEW" on a refreshed variant's own name, confirmed on a real Mahindra
    generation-refresh masters sheet). Residue-derivation here would
    produce "NEWZ4" for the master side, which doesn't prefix/suffix-match
    the Booking Form's own "Z4" trim residue at all -- only the real trim
    field lets this match."""
    row = _row(
        "SCORPIO N", "NEW Z4 G MT 2WD 7 STR - E BS6.2",
        fuel="PETROL", transmission="MT", drive="2WD", seater="7", trim="Z4", sku="Z4NEW",
    )
    matched = m.match_by_attributes(
        [row], oem_code="MAHINDRA", model_remainder="Z4", variant_text="G MT 2WD 7STR"
    )
    assert [r["sku_code"] for r in matched] == ["Z4NEW"]


def test_falls_back_to_variant_name_residue_when_no_trim_field_stored() -> None:
    # A variant ingested before oem_price_masters.py carried trim through
    # (or not yet re-uploaded) -- must keep working exactly as before.
    row = _row(
        "SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - N",
        fuel="DIESEL", transmission="AT", drive="2WD", seater="7", sku="Z8S",
    )
    matched = m.match_by_attributes(
        [row], oem_code="MAHINDRA", model_remainder="Z8 (S)", variant_text="DAT 2WD 7STR"
    )
    assert [r["sku_code"] for r in matched] == ["Z8S"]


def test_master_row_derives_fuel_transmission_drive_seater_from_variant_name_when_columns_blank() -> None:
    """Real Mahindra Consolidated Price List exports routinely leave
    fuel_powertrain/transmission/drive/seater blank per-row even though the
    header names them -- the same information is already carried in the
    free-text Variant column instead (e.g. "Z8 L G MT 2WD 7 STR" already
    states fuel=G, transmission=MT, drive=2WD, seater=7). Without deriving
    these from variant_name the same way trim already falls back to it, an
    MT and an AT candidate for the same trim would both silently pass
    (blank master column skips the check instead of failing it) and the
    Booking Form's own transmission would never disambiguate them.
    """
    rows = [
        _row("NEW SCORPIO N", "Z8 L G MT 2WD 7 STR", sku="Z8L_MT"),
        _row("NEW SCORPIO N", "Z8 L G AT 2WD 7 STR", sku="Z8L_AT"),
    ]
    matched = m.match_by_attributes(
        rows, oem_code="MAHINDRA", model_remainder="Z8 L", variant_text="G AT 2WD 7STR"
    )
    assert [r["sku_code"] for r in matched] == ["Z8L_AT"]


def test_unknown_oem_is_a_no_op() -> None:
    rows = [_row("SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7")]
    matched = m.match_by_attributes(
        rows, oem_code="HYUNDAI", model_remainder="Z8 (S)", variant_text="DAT 2WD 7STR"
    )
    assert matched == []


# ── has_qualifying_signal ─────────────────────────────────────────────────────
def test_has_qualifying_signal_true_for_a_recognized_fuel_token() -> None:
    assert m.has_qualifying_signal(oem_code="MAHINDRA", model_remainder="", variant_text="Z8T D AT")


def test_has_qualifying_signal_false_for_a_bare_trim_code() -> None:
    # "Z8L" alone carries no fuel/transmission/drive/seater fact -- it must
    # not be treated as more reliable than an exact price match.
    assert not m.has_qualifying_signal(oem_code="MAHINDRA", model_remainder="", variant_text="Z8L")


def test_has_qualifying_signal_false_when_nothing_supplied() -> None:
    assert not m.has_qualifying_signal(oem_code="MAHINDRA", model_remainder="", variant_text=None)


def test_has_qualifying_signal_false_for_unknown_oem() -> None:
    assert not m.has_qualifying_signal(oem_code="HYUNDAI", model_remainder="", variant_text="D AT")


def test_has_qualifying_signal_true_for_trim_that_genuinely_discriminates() -> None:
    # Trim is one of the six things that should count as real signal, not
    # just fuel/transmission/drive/seater -- but only when the candidate
    # pool actually has more than one distinct trim to choose between.
    rows = [
        _row("SCORPIO N", "Z8S G MT 2WD 7 STR BS6.2 - N", fuel="PETROL", transmission="MT", drive="2WD", seater="7"),
        _row("SCORPIO N", "Z8T G MT 2WD 7 STR BS6.2 - N", fuel="PETROL", transmission="MT", drive="2WD", seater="7"),
    ]
    assert m.has_qualifying_signal(
        oem_code="MAHINDRA", model_remainder="", variant_text="Z8T", candidate_rows=rows
    )


def test_has_qualifying_signal_false_for_a_bare_trim_with_only_one_candidate() -> None:
    # The exact case that broke a naive "any non-empty trim counts" draft:
    # a single leftover candidate trivially "matches" a bare trim code that
    # was never actually disambiguating anything.
    rows = [
        _row("SCORPIO N", "Z8L G MT 2WD 7 STR BS6.2 - N - ADAS", fuel="PETROL", transmission="MT", drive="2WD", seater="7"),
    ]
    assert not m.has_qualifying_signal(
        oem_code="MAHINDRA", model_remainder="", variant_text="Z8L", candidate_rows=rows
    )


def test_has_qualifying_signal_true_using_the_real_trim_field_not_residue() -> None:
    # The exact live scenario reported: Z2/Z4/Z8 S/Z8T/Z8 L on the same
    # model share an identical fuel/transmission/drive/seater combination --
    # only their real Trim actually tells them apart.
    rows = [
        _row("SCORPIO N", "Z4 G MT 2WD 7 STR - E BS6.2 - New", fuel="PETROL", transmission="MT", drive="2WD", seater="7", trim="Z4"),
        _row("SCORPIO N", "Z8 S G MT 2WD 7 STR BS6.2 - Refresh", fuel="PETROL", transmission="MT", drive="2WD", seater="7", trim="Z8 S"),
    ]
    assert m.has_qualifying_signal(
        oem_code="MAHINDRA", model_remainder="", variant_text="Z4", candidate_rows=rows
    )


def test_has_qualifying_signal_false_when_every_candidate_shares_the_same_trim() -> None:
    # Same trim code across the whole pool (they differ only by
    # transmission) -- trim itself carries no discriminating power here.
    rows = [
        _row("SCORPIO N", "Z8T D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7"),
        _row("SCORPIO N", "Z8T D MT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="MT", drive="2WD", seater="7"),
    ]
    assert not m.has_qualifying_signal(
        oem_code="MAHINDRA", model_remainder="Z8T", variant_text=None, candidate_rows=rows
    )


# ── fuzzy_match_by_attributes (controlled fuzzy fallback) ───────────────────
def test_fuzzy_resolves_a_single_mid_string_ocr_misread_in_trim() -> None:
    # Booking Form's trim text was OCR'd as "Z9S" ("8" misread as "9") --
    # exact match_by_attributes finds nothing (neither "Z9S" nor "Z8S"/
    # "Z8T" is a prefix/suffix of the other), but "Z9S" is genuinely,
    # unambiguously closer to "Z8S" than to "Z8T" (SequenceMatcher: 0.67
    # vs 0.33 -- both differ by one character, but Z8S's differing
    # character is adjacent to two matching ones, Z8T's isn't). Z8L6 is
    # excluded outright regardless, on a hard seater mismatch (6 vs the
    # Booking Form's stated 7STR). Master rows carry their own real Trim
    # column, as a properly-ingested master does -- the variant-name-
    # residue fallback is noisier (picks up "- N"/"- Refresh" generation
    # suffixes) and is exercised separately below.
    rows = [
        _row("SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7", trim="Z8 S", sku="Z8S"),
        _row("SCORPIO N", "Z8T D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7", trim="Z8T", sku="Z8T"),
        _row("SCORPIO N", "Z8 L D AT 2WD 6 STR BS6.2 - N - ADAS", fuel="DIESEL", transmission="AT", drive="2WD", seater="6", trim="Z8 L", sku="Z8L6"),
    ]
    assert m.match_by_attributes(
        rows, oem_code="MAHINDRA", model_remainder="Z9S", variant_text="DAT 2WD 7STR"
    ) == []

    candidates = m.fuzzy_match_by_attributes(
        rows, oem_code="MAHINDRA", model_remainder="Z9S", variant_text="DAT 2WD 7STR"
    )
    assert [c.row["sku_code"] for c in candidates] == ["Z8S", "Z8T"]
    assert candidates[0].score >= 0.70
    assert candidates[0].score - candidates[1].score >= 0.15


def test_fuzzy_does_not_guess_between_two_genuinely_close_trims() -> None:
    # "Z8" alone, with nothing else stated, is genuinely equidistant
    # between Z8S and Z8T -- must not silently pick one.
    rows = [
        _row("SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7", sku="Z8S"),
        _row("SCORPIO N", "Z8 T D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7", sku="Z8T"),
    ]
    candidates = m.fuzzy_match_by_attributes(
        rows, oem_code="MAHINDRA", model_remainder="Z8", variant_text="DAT 2WD 7STR"
    )
    assert len(candidates) == 2
    assert abs(candidates[0].score - candidates[1].score) < 0.15


def test_fuzzy_still_disqualifies_a_stated_attribute_mismatch() -> None:
    # Perfect trim text match, but the Booking Form states Diesel and this
    # row is Petrol -- attributes stay a hard, exact filter under fuzzy
    # matching too, never merely a lower score.
    rows = [
        _row("SCORPIO N", "Z8 S G AT 2WD 7 STR BS6.2 - N", fuel="PETROL", transmission="AT", drive="2WD", seater="7", sku="Z8SPETROL"),
    ]
    candidates = m.fuzzy_match_by_attributes(
        rows, oem_code="MAHINDRA", model_remainder="Z8S", variant_text="D AT 2WD 7STR"
    )
    assert candidates == []


def test_fuzzy_confirmed_attributes_break_a_tie_between_equal_trim_similarity() -> None:
    # Both rows have an identical trim residue once decomposed ("Z8S"), but
    # only one of them also has its own transmission column populated and
    # agreeing with the Booking Form -- that row is more corroborated.
    rows = [
        _row("SCORPIO N", "Z8 S D 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission=None, drive="2WD", seater="7", sku="NO_TRANS_COL"),
        _row("SCORPIO N", "Z8 S D AT 2WD 7 STR BS6.2 - N", fuel="DIESEL", transmission="AT", drive="2WD", seater="7", sku="HAS_TRANS_COL"),
    ]
    candidates = m.fuzzy_match_by_attributes(
        rows, oem_code="MAHINDRA", model_remainder="Z8S", variant_text="D AT 2WD 7STR"
    )
    assert candidates[0].row["sku_code"] == "HAS_TRANS_COL"
    assert candidates[0].score > candidates[1].score
