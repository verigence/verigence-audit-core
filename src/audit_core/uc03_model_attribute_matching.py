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
     variant-name "trim residue" (the same tokenizer applied to the master's
     own ``variant_name``, with every recognized token removed) is a
     normalized prefix/suffix of the Booking Form's own trim residue.

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
        if booking.fuel and row.get("fuel_powertrain") and booking.fuel != str(row["fuel_powertrain"]).upper():
            continue
        if (
            booking.transmission
            and row.get("transmission")
            and booking.transmission != str(row["transmission"]).upper()
        ):
            continue
        if booking.drive and row.get("drive") and booking.drive != str(row["drive"]).upper():
            continue
        if booking.seater and row.get("seater") and booking.seater != str(row["seater"]):
            continue

        master = _decompose(str(row.get("variant_name") or ""), oem_code=oem_code)
        master_key = master.trim_key if master else ""
        if not master_key:
            continue
        if not (booking.trim_key.startswith(master_key) or master_key.startswith(booking.trim_key)):
            continue
        matched.append(row)
    return matched
