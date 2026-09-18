"""uc03_model_attribute_matching.py — OEM-specific fallback for
``uc03_model_resolution``'s exact whole-string SKU matcher.

Real Booking Forms squeeze model + trim + fuel + transmission + drivetrain +
seating (and sometimes an emission-norm code) into one or two handwritten
fields, e.g. ``"SCORPIO N Z8 (S)"`` / ``"DAT 2WD 7STR"`` or
``"XUV-7XO"`` / ``"AX-7L(D) AT 7ST8"``. A whole-string equality match against
the price master's ``model_name`` never succeeds against text like this — the
master's own ``model_name`` is just ``"SCORPIO N"``; the trim/fuel/
transmission/drive/seater live on the *variant* row as separate, already-clean
attributes (``product_variants.fuel_powertrain`` / ``.transmission`` /
``.attributes->>'drive'`` / ``.attributes->>'seater'``, populated verbatim
from the OEM's own price-list columns by ``oem_price_masters.py``).

This module never introduces fuzzy text matching — every comparison here is
still exact, on a decomposed signal instead of the whole string:

  1. Resolve which master *model* the free text names, via
     ``oem_model_aliases`` (OEM-scoped, the same table ``oem_price_masters.py``
     uses at ingestion time) — the longest normalized prefix match, not a
     fuzzy score.
  2. Tokenize whatever text is left and pull out any fuel/transmission/drive/
     seater tokens it recognizes, via a small per-OEM vocabulary. Only
     Mahindra's is implemented; an unrecognized OEM code makes this whole
     fallback a no-op (``uc03_model_resolution`` keeps its current
     zero-candidate behaviour for it).
  3. Filter that model's variants to the ones whose own structured attributes
     agree with every signal the Booking Form actually supplied, and whose
     trim is a normalized prefix/suffix of the Booking Form's own trim
     residue -- the master's real, stored Trim
     (``product_variants.attributes->>'trim'``, the masters sheet's own Trim
     column, populated by ``oem_price_masters.py``) when present, falling
     back to a variant-name "trim residue" (the same tokenizer applied to
     the master's own ``variant_name``, with every recognized token removed)
     only for a variant ingested before that column existed.

A signal the Booking Form never supplied is never used to eliminate a
candidate — this only adds evidence, it never invents it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def _words(text: str) -> list[str]:
    """Uppercase alnum-run tokens, e.g. ``'AX-7L(D)'`` -> ``['AX', '7L', 'D']``."""
    return [w for w in _NON_ALNUM.sub(" ", text.upper()).split() if w]


def normalized_model_key(text: str | None) -> str:
    """Glued, uppercase, punctuation/whitespace-insensitive form of a model
    name, e.g. ``'Scorpio Classic'`` / ``'SCORPIO-CLASSIC'`` / ``'SCORPIO
    CLASSIC'`` all -> ``'SCORPIOCLASSIC'``.

    ``resolve_model_via_aliases`` already compares text this way to find the
    matching alias, but the canonical name it returns (verbatim from
    ``oem_model_aliases``, a manually seeded reference table) was then being
    compared with a raw ``==`` against a price-master row's own ``model_name``
    (verbatim from the OEM's price-list ingestion) -- two independently
    authored strings with no guarantee of matching casing/spacing. Both
    sides must be normalized the same way before comparing, or a real
    alias-resolved model can still fail to find any of its own master rows.
    """
    return "".join(_words(text or ""))


# ── per-OEM vocabulary ──────────────────────────────────────────────────────
# Only tokens actually observed in Mahindra's ingested price-master
# variant_name text and on real Booking Forms (verified directly against the
# ingested masters). Extend empirically as new forms surface — an
# unrecognized token is left in the trim residue, never guessed away.
_MAHINDRA_FUEL_TOKENS: dict[str, str] = {
    "D": "DIESEL", "DS": "DIESEL", "DSL": "DIESEL", "DIESEL": "DIESEL",
    "G": "PETROL", "GM": "PETROL", "PM": "PETROL", "P": "PETROL",
    "PET": "PETROL", "PETROL": "PETROL", "PMS": "PETROL", "TGDI": "PETROL",
    "CNG": "CNG",
    "EV": "ELECTRIC", "ELECTRIC": "ELECTRIC",
}
_MAHINDRA_TRANSMISSION_TOKENS: dict[str, str] = {
    "MT": "MT", "MANUAL": "MT",
    "AT": "AT", "AS": "AT", "AUTO": "AT", "AUTOMATIC": "AT",
}
_MAHINDRA_DRIVE_TOKENS: dict[str, str] = {
    "2WD": "2WD", "4WD": "4WD", "AWD": "AWD", "FWD": "2WD",
}
# Fuel + transmission glued with no separator, e.g. "DAT" (Diesel + AT) —
# seen on real Booking Forms even though the master's own text never writes
# it that way. Group 1 is a fuel-letter code, group 2 a transmission code.
_MAHINDRA_COMPOUND_RE = re.compile(r"^(DS|D|G|P|C)(AT|MT|AS)$")
_SEATER_RE = re.compile(r"^(\d)(STR|ST|SEATER|SEAT)$")
# "BS6.2" tokenizes as two words ("BS6", "2") once "." is treated as a
# separator, so the emission-norm's decimal component is consumed as a
# trailing bare digit immediately after the "BS<n>" word, not matched in
# one regex.
_BS_NORM_RE = re.compile(r"^BS\d{1,2}$")

_VOCAB_BY_OEM: dict[str, dict[str, Any]] = {
    "MAHINDRA": {
        "fuel": _MAHINDRA_FUEL_TOKENS,
        "transmission": _MAHINDRA_TRANSMISSION_TOKENS,
        "drive": _MAHINDRA_DRIVE_TOKENS,
        "compound_re": _MAHINDRA_COMPOUND_RE,
    },
}


@dataclass(frozen=True)
class _Decomposed:
    fuel: str | None
    transmission: str | None
    drive: str | None
    seater: str | None
    trim_key: str  # normalized, no separators — whatever wasn't recognized


def _decompose(text: str | None, *, oem_code: str) -> _Decomposed | None:
    """Pull recognized fuel/transmission/drive/seater tokens out of ``text``;
    whatever's left (in order, glued with no separator) is the trim residue.
    None if this OEM has no vocabulary or ``text`` is empty."""
    if not text:
        return None
    vocab = _VOCAB_BY_OEM.get(oem_code)
    if vocab is None:
        return None

    fuel: str | None = None
    transmission: str | None = None
    drive: str | None = None
    seater: str | None = None
    residue: list[str] = []

    words = _words(text)
    i = 0
    while i < len(words):
        word = words[i]
        compound = vocab["compound_re"].match(word)
        if compound:
            fuel = fuel or vocab["fuel"].get(compound.group(1))
            transmission = transmission or vocab["transmission"].get(compound.group(2))
            i += 1
            continue
        if word in vocab["fuel"]:
            fuel = fuel or vocab["fuel"][word]
            i += 1
            continue
        if word in vocab["transmission"]:
            transmission = transmission or vocab["transmission"][word]
            i += 1
            continue
        if word in vocab["drive"]:
            drive = drive or vocab["drive"][word]
            i += 1
            continue
        seater_match = _SEATER_RE.match(word)
        if seater_match:
            seater = seater or seater_match.group(1)
            i += 1
            continue
        if word.isdigit() and i + 1 < len(words) and words[i + 1] in ("STR", "SEATER", "SEAT", "ST"):
            seater = seater or word
            i += 2
            continue
        if _BS_NORM_RE.match(word):
            i += 1
            # swallow the emission norm's decimal component too, e.g. the
            # "2" in "BS6.2" once "." has already split it off as its own word
            if i < len(words) and words[i].isdigit() and len(words[i]) <= 2:
                i += 1
            continue
        residue.append(word)
        i += 1

    return _Decomposed(
        fuel=fuel, transmission=transmission, drive=drive, seater=seater,
        trim_key="".join(residue),
    )


def _master_trim_key(row: dict[str, Any], *, oem_code: str) -> str:
    """A master variant row's own trim, normalized -- the authoritative
    value from the masters sheet's own Trim column
    (``product_variants.attributes->>'trim'``, populated by
    ``oem_price_masters.py``, selected as ``row["trim"]`` by
    ``_sku_rows_for_version``) when present. Falls back to the residue-
    derived trim_key (whatever's left in ``variant_name`` after removing
    every recognized fuel/transmission/drive/seater token) only for a
    variant ingested before that column existed and not yet re-uploaded --
    the fallback this whole module originally relied on, kept only as a
    transition path, not the primary signal anymore.
    """
    real_trim = row.get("trim")
    if real_trim:
        glued = "".join(_words(str(real_trim)))
        if glued:
            return glued
    master = _decompose(str(row.get("variant_name") or ""), oem_code=oem_code)
    return master.trim_key if master else ""


def _master_attributes(row: dict[str, Any], *, oem_code: str) -> _Decomposed:
    """A master variant row's own fuel/transmission/drive/seater, preferring the
    masters sheet's own dedicated columns (``fuel_powertrain``/``transmission``/
    ``drive``/``seater``, populated by ``oem_price_masters.py``) but falling back
    -- independently, per attribute, same as ``_master_trim_key`` already does for
    trim alone -- to decomposing the row's own ``variant_name`` for whichever the
    master left blank.

    Real Mahindra Consolidated Price List exports routinely leave these columns
    blank per-row even though the header names them: the same information is
    already carried in the free-text Variant column instead (e.g. "Z8 S G AT 2WD
    7 STR BS6.2 - Refresh" encodes fuel=G, transmission=AT, drive=2WD, seater=7).
    Without this fallback, ``match_by_attributes`` below silently skips every
    blank attribute's check instead of deriving it, degrading matching to
    trim-only and letting otherwise-distinct variants collide.
    """
    fallback = _decompose(str(row.get("variant_name") or ""), oem_code=oem_code)

    def pick(raw_value: Any, fallback_value: str | None) -> str | None:
        if raw_value:
            return str(raw_value).upper()
        return fallback_value

    seater_raw = row.get("seater")
    return _Decomposed(
        fuel=pick(row.get("fuel_powertrain"), fallback.fuel if fallback else None),
        transmission=pick(row.get("transmission"), fallback.transmission if fallback else None),
        drive=pick(row.get("drive"), fallback.drive if fallback else None),
        seater=str(seater_raw) if seater_raw else (fallback.seater if fallback else None),
        trim_key=_master_trim_key(row, oem_code=oem_code),
    )


# ── model resolution via oem_model_aliases ──────────────────────────────────
def resolve_model_via_aliases(
    *, model_name: str, oem_aliases: list[tuple[str, str]]
) -> tuple[str, str] | None:
    """Longest normalized-prefix match of ``model_name`` against
    ``[(alias_text, canonical_model_name), ...]``.

    Matching is done on the fully glued (no-separator) normalized form so
    that punctuation differences between the alias spelling and the Booking
    Form's own (``"XUV7XO"`` vs ``"XUV-7XO"``) don't block the match. Returns
    ``(canonical_model_name, remainder_text)`` — the remainder is whatever
    follows the matched alias, for further trim/attribute decomposition — or
    None if no alias is a prefix of ``model_name``.
    """
    text_words = _words(model_name)
    glued = "".join(text_words)
    if not glued:
        return None

    best: tuple[int, str] | None = None  # (alias_key length, canonical)
    for alias_text, canonical in oem_aliases:
        alias_key = "".join(_words(alias_text))
        if not alias_key:
            continue
        if glued.startswith(alias_key) and (best is None or len(alias_key) > best[0]):
            best = (len(alias_key), canonical)
    if best is None:
        return None
    alias_len, canonical = best

    consumed = len(text_words)
    acc_len = 0
    for idx, word in enumerate(text_words):
        acc_len += len(word)
        if acc_len >= alias_len:
            consumed = idx + 1
            break
    return canonical, " ".join(text_words[consumed:])


def has_qualifying_signal(
    *,
    oem_code: str,
    model_remainder: str,
    variant_text: str | None,
    candidate_rows: list[dict[str, Any]] | None = None,
) -> bool:
    """True when the combined text states model + trim + at least one real,
    exact-valued fact -- all six of model, trim, fuel, transmission, drive,
    seater are in play, not only the last four.

    Model is already handled upstream (``resolve_model_via_aliases`` is
    what got the caller this far at all). Fuel/transmission/drive/seater
    each qualify on their own -- they are exact-value facts about the
    specific vehicle.

    Trim only qualifies when it is doing real work: ``candidate_rows`` (the
    same pool ``match_by_attributes`` is about to filter) must contain more
    than one *distinct* trim residue to choose between. A bare trim code
    with nothing else, filtered against a candidate pool that only ever
    had one trim to begin with, "matches" via ``match_by_attributes``'
    trim-residue prefix check purely because there was nothing to eliminate
    it with -- that is no more certain than a plain name/variant equality
    check, and must not be allowed to outrank an exact price match. Confirmed
    this is a real, not theoretical, distinction: hand-tracing an early
    version of this function's "any non-empty trim counts" against a real
    Mahindra generation-refresh case showed the lone remaining candidate
    trivially "matching" a bare trim code that was never actually
    disambiguating anything.
    """
    combined = " ".join(w for w in (model_remainder, variant_text) if w)
    decomposed = _decompose(combined, oem_code=oem_code)
    if decomposed is None:
        return False
    if decomposed.fuel or decomposed.transmission or decomposed.drive or decomposed.seater:
        return True
    if not decomposed.trim_key or not candidate_rows:
        return False
    master_trim_keys = {
        key
        for key in (_master_trim_key(row, oem_code=oem_code) for row in candidate_rows)
        if key
    }
    return len(master_trim_keys) > 1


# ── attribute-filtered variant matching ─────────────────────────────────────
def match_by_attributes(
    rows: list[dict[str, Any]],
    *,
    oem_code: str,
    model_remainder: str,
    variant_text: str | None,
) -> list[dict[str, Any]]:
    """``rows`` already narrowed to one resolved model. Decompose the
    Booking Form's remaining text and keep only variants whose own
    structured attributes agree with every signal supplied, and whose trim
    residue is a normalized prefix/suffix match — never a fuzzy score."""
    combined = " ".join(w for w in (model_remainder, variant_text) if w)
    booking = _decompose(combined, oem_code=oem_code)
    if booking is None or not booking.trim_key:
        return []

    matched: list[dict[str, Any]] = []
    for row in rows:
        master = _master_attributes(row, oem_code=oem_code)
        if booking.fuel and master.fuel and booking.fuel != master.fuel:
            continue
        if booking.transmission and master.transmission and booking.transmission != master.transmission:
            continue
        if booking.drive and master.drive and booking.drive != master.drive:
            continue
        if booking.seater and master.seater and booking.seater != master.seater:
            continue

        master_key = master.trim_key
        if not master_key:
            continue
        if not (booking.trim_key.startswith(master_key) or master_key.startswith(booking.trim_key)):
            continue
        matched.append(row)
    return matched
