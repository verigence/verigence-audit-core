"""uc03_sku_words.py -- name a price-master SKU from the words one document
prints (product owner rules 2026-10-20, R3 and R4).

The rules this module implements:

* The SKU is identified by its words: model + trim + variant, after the
  spelling normalisation below, with an exact match of the distinguishing
  words in both directions. Exactly one price-master row = the
  word-identified SKU. Anything else is a PC task with a shortlist.
* Price never chooses a variant, a fuel, a gearbox, Private/Commercial or
  EV/ICE. Nothing in this module reads a price except ``order_shortlist``,
  which only orders a shortlist the PC picks from, and ``price_check``,
  which compares an already word-identified SKU's master price with the
  document's.
* The vehicle category comes first and must be certain: EV vs ICE (EV
  models, the EV/ELECTRIC word, battery kWh / B59 / B70 / B79, a 5% GST
  invoice) and Private vs Commercial (the registration type). Uncertain
  means a PC task, never a pin.

Pure functions only (no database), so the same code runs in the resolver,
in the tests and in the harness that replays real price lists.

Normalisation table (retro review sku.md section 5):

* Fuel: D / DS / DSL / DIESEL; P / PM / PET / PETROL / G / GM / PG / TGDI /
  MSTALLION; CNG; EV / ELECTRIC / BEV. Glued fuel+gearbox (DAT, DMT, PAT,
  PMT, GAT) is read as both.
* Gearbox: MT / MANUAL; AT / AS / AUTO / AUTOMATIC / AMT (the list's "AS"
  is Mahindra's AutoShift AMT).
* Trim suffixes: "AX5 L" -> AX5L, "Z8 L" -> Z8L, "Z8 (S)" / "Z8 S" -> Z8S,
  "EDGE(O)" -> EDGEO, "M(O)" -> MO; "AX-7L" -> AX7L. "(D)" / "(P)" stay fuel.
* Displacement: "1.7 L" and "1.7L" -> 1.7L, but "1.7" stays 1.7 (the Maxx
  HD list prices 1.7 and 1.7L as different vehicles). "1.5XXL" -> 1.5 XXL.
* LXD -> LX + diesel; LXI -> LX + petrol. VXI / VXi are the same word.
* EV: "PACK ONE" -> ONE; "59 kWh" -> B59; a bare 59/70/79 reads as the
  battery when the model's list uses B59/B70/B79.
* Model words are stripped on both sides ("BE 6" in the list's variant
  text against "BE6" on the document).
* Noise never decides: BS6 / BS6.2 / BSVI, Refresh, New, the "E" edition
  tag, Private / Commercial (the registration is decided separately).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

# Paise rounding between a booking form or invoice and the price master:
# two rupees either way is the same price (2026-10-20 product owner rules).
PRICE_TOLERANCE = Decimal(2)

_FUEL: dict[str, str] = {
    "D": "DIESEL", "DS": "DIESEL", "DSL": "DIESEL", "DIESEL": "DIESEL",
    "P": "PETROL", "PM": "PETROL", "PET": "PETROL", "PETROL": "PETROL", "G": "PETROL", "GM": "PETROL",
    "PG": "PETROL", "PMS": "PETROL", "TGDI": "PETROL", "MSTALLION": "PETROL",
    "CNG": "CNG",
    "EV": "ELECTRIC", "ELECTRIC": "ELECTRIC", "BEV": "ELECTRIC",
}
_ICE_FUELS = frozenset({"DIESEL", "PETROL", "CNG"})
_TRANSMISSION: dict[str, str] = {
    "MT": "MT", "MANUAL": "MT",
    "AT": "AT", "AS": "AT", "AUTO": "AT", "AUTOMATIC": "AT", "AMT": "AT",
}
_DRIVE: dict[str, str] = {"2WD": "2WD", "4WD": "4WD", "AWD": "AWD", "FWD": "2WD", "RWD": "2WD",
                          "4X4": "4WD", "4X2": "2WD"}
_COMPOUND = re.compile(r"^(D|DS|P|PM|G)(AT|MT|AS|AMT)$")
_SEATER = re.compile(r"^(\d{1,2})(?:STR|STRS|ST|SEATER|SEATERS|SEATS|SEAT|S)$")
_SEATER_WORDS = frozenset({"STR", "STRS", "ST", "SEATER", "SEATERS", "SEATS", "SEAT"})
_LX_FUEL = {"LXD": ("LX", "DIESEL"), "LXI": ("LX", "PETROL")}
# Never a distinguishing word. Release markers ("Refresh", "New") are not
# here (review #471 S2): like "AC", such a word is optional only when no
# other row of the model is the same vehicle without it.
_NOISE = frozenset({
    "E", "PRIVATE", "COMMERCIAL", "PACK", "BATTERY", "CHARGER", "KWH", "KW",
    "MAHINDRA", "WITH", "AND", "THE",
})
# A trim code a lone L or S belongs to: AX5 L, Z8 L, Z8 S, AX7 L.
_TRIM_CODE = re.compile(r"^[A-Z]{1,3}\d{1,2}$")
_GLUE_SUFFIX = frozenset({"L", "S"})
# A word with letters and digits (AX9, W7L, Z10) the list does not know: a
# misread or a variant the list lacks. It blocks a word match rather than
# being ignored like a colour or "1.2".
_TRIM_LIKE = re.compile(r"^(?=[A-Z0-9+]*[A-Z])(?=[A-Z0-9+]*\d)[A-Z0-9+]+$")
_TOKEN = re.compile(r"[A-Z]*\d+\.\d+[A-Z]*|[A-Z0-9]+\+?")
_ATTRIBUTE_KEYS = ("fuel", "transmission", "drive", "seater")
_ATTRIBUTE_PREFIX = {"fuel": "F:", "transmission": "T:", "drive": "D:", "seater": "S:"}
_EV_MODEL = re.compile(r"^(BE ?6|XEV|XUV ?400)|\bEV$", re.IGNORECASE)


def normalized_key(value: str | None) -> str:
    """Glued, upper-case letters and digits ("XUV 3XO" -> "XUV3XO")."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def generation_key(model_name: str | None) -> str:
    """The nameplate without the generation words: "NEW THAR 2WD & 4WD",
    "NEW THAR" and "THAR" are one family; "THAR ROXX" is not."""
    key = normalized_key(model_name)
    if key.startswith("NEW") and len(key) > 5:
        key = key[3:]
    return re.sub(r"(?:2WD|4WD|AWD)+$", "", key) or key


def _prepare(text_value: str | None) -> str:
    text_value = str(text_value or "").upper()
    text_value = re.sub(r"\bBS\s*-?\s*(?:VI|6)(?:\.\d)?", " ", text_value)
    text_value = re.sub(r"\s*\(\s*([OSLT])\s*\)", r"\1", text_value)
    text_value = re.sub(r"(\d+(?:\.\d+)?)\s*KWH\b", r" B\1 ", text_value)
    text_value = re.sub(r"(\d+(?:\.\d+)?)\s*KW\b", r" \1KW ", text_value)
    text_value = re.sub(r"(\d+\.\d+)\s*L\b", r"\1L", text_value)
    text_value = re.sub(r"(\d+\.\d+)([A-Z]{2,})", r"\1 \2", text_value)
    text_value = re.sub(r"\b([A-Z]{1,3})-(\d)", r"\1\2", text_value)
    return text_value


def raw_tokens(text_value: str | None) -> list[str]:
    """The words as printed, after the spelling normalisation."""
    tokens = _TOKEN.findall(_prepare(text_value))
    out: list[str] = []
    for token in tokens:
        if token in _GLUE_SUFFIX and out and _TRIM_CODE.match(out[-1]):
            out[-1] = out[-1] + token
            continue
        out.append(token)
    return out


def _strip_model_prefix(tokens: list[str], model_name: str | None) -> list[str]:
    """Drop the leading words that spell the model ("BE 6 Two ..." under the
    model "BE 6"; "XUV3XO EV - AX5" under "XUV 3XO EV")."""
    key = normalized_key(model_name)
    if not key:
        return tokens
    glued = ""
    for index, token in enumerate(tokens):
        glued += token.replace("+", "")
        if glued == key:
            return tokens[index + 1:]
        if not key.startswith(glued):
            break
    return tokens


def _model_words(model_name: str | None) -> set[str]:
    """Every run of the model name's own words, glued ("SCORPIO", "N",
    "SCORPION" for "SCORPIO N")."""
    parts = [normalized_key(p) for p in re.split(r"[^A-Za-z0-9]+", str(model_name or "")) if normalized_key(p)]
    words: set[str] = set()
    for start in range(len(parts)):
        for end in range(start + 1, len(parts) + 1):
            words.add("".join(parts[start:end]))
    return words


@dataclass(frozen=True)
class Reading:
    """The distinguishing words of one text: trim words and the attribute
    values it states."""

    trim: frozenset[str]
    attrs: dict[str, str | None]
    ev_word: bool = False
    fuels: frozenset[str] = frozenset()  # every fuel the text names (two = a contradiction)
    # Attributes the text states twice with different values ("2WD" and
    # "4WD", two seat counts): a contradiction, never first-wins.
    conflicts: frozenset[str] = frozenset()

    def tokens(self) -> frozenset[str]:
        return frozenset(self.trim) | frozenset(
            _ATTRIBUTE_PREFIX[k] + v for k, v in self.attrs.items() if v
        )


def read_words(tokens: list[str], *, model_name: str | None) -> Reading:
    """Split already-tokenised text (model prefix removed) into attribute
    values and trim words; the model's own words are not trim words."""
    attrs: dict[str, str | None] = dict.fromkeys(_ATTRIBUTE_KEYS)
    trim: set[str] = set()
    ev_word = False
    fuels: set[str] = set()
    seen: dict[str, set[str]] = {k: set() for k in _ATTRIBUTE_KEYS}
    model_words = _model_words(model_name)

    def set_attr(key: str, value: str) -> None:
        if key == "fuel":
            fuels.add(value)
        seen[key].add(value)
        if attrs[key] is None:
            attrs[key] = value

    index = 0
    while index < len(tokens):
        token = tokens[index]
        nxt = tokens[index + 1] if index + 1 < len(tokens) else None
        index += 1
        if token in _NOISE:
            continue
        if token in _LX_FUEL:
            trim_word, fuel = _LX_FUEL[token]
            trim.add(trim_word)
            set_attr("fuel", fuel)
            continue
        compound = _COMPOUND.match(token)
        if compound:
            set_attr("fuel", _FUEL[compound.group(1)])
            set_attr("transmission", _TRANSMISSION[compound.group(2)])
            continue
        if token in _FUEL:
            set_attr("fuel", _FUEL[token])
            ev_word = ev_word or _FUEL[token] == "ELECTRIC"
            continue
        if token in _TRANSMISSION:
            set_attr("transmission", _TRANSMISSION[token])
            continue
        if token in _DRIVE:
            set_attr("drive", _DRIVE[token])
            continue
        seater = _SEATER.match(token)
        if seater:
            set_attr("seater", str(int(seater.group(1))))
            continue
        if token.isdigit() and nxt in _SEATER_WORDS:
            set_attr("seater", str(int(token)))
            index += 1
            continue
        if token in _SEATER_WORDS:
            continue
        if token.replace("+", "") in model_words:
            continue
        trim.add(token)
    conflicts = frozenset(k for k, values in seen.items() if len(values) > 1 and k != "fuel")
    return Reading(trim=frozenset(trim), attrs=attrs, ev_word=ev_word, fuels=frozenset(fuels),
                   conflicts=conflicts)


def drop_model_runs(tokens: list[str], spellings: list[str | None]) -> list[str]:
    """Take out every run of words that spells the model (any of its
    spellings), wherever it stands: a variant box that repeats the model
    ("NEW THAR 2WD & 4WD LXT D MT 4WD", "XEV 9S ONE ABOVE ... 7STR") must not
    read the model's own "2WD" or "9S" as the vehicle's drive or seats
    (review #471 S1)."""
    keys = sorted({normalized_key(sp) for sp in spellings if normalized_key(sp)}, key=len, reverse=True)
    if not keys:
        return tokens
    out: list[str] = []
    index = 0
    while index < len(tokens):
        for key in keys:
            glued, end = "", index
            while end < len(tokens) and key.startswith(glued + tokens[end].replace("+", "")):
                glued += tokens[end].replace("+", "")
                end += 1
                if glued == key:
                    break
            if glued == key and end > index:
                index = end
                break
        else:
            out.append(tokens[index])
            index += 1
    return out


# ── the price-master side ─────────────────────────────────────────────────────
def row_registration(row: dict[str, Any]) -> str | None:
    """The registration a price-list row is for, when the list prices the
    same vehicle twice (variant "(Private)" / "(Commercial)", code ending
    ::PRI / ::COM)."""
    code = str(row.get("sku_code") or "").upper()
    variant = str(row.get("variant_name") or "").upper()
    if code.endswith("::COM") or variant.endswith("(COMMERCIAL)"):
        return "COMMERCIAL"
    if code.endswith("::PRI") or variant.endswith("(PRIVATE)"):
        return "PRIVATE"
    return None


def row_is_ev(row: dict[str, Any]) -> bool:
    fuel = str(row.get("fuel_powertrain") or "").upper()
    category = str(row.get("category") or "").upper()
    segment = str(row.get("segment_code") or "").upper()
    return (
        fuel in {"ELECTRIC", "EV", "BEV"}
        or category in {"BEV", "EV"}
        or segment == "BATTERY_ELECTRIC"
        or bool(_EV_MODEL.search(str(row.get("model_name") or "").strip()))
    )


@dataclass(frozen=True)
class RowWords:
    row: dict[str, Any]
    words: frozenset[str]  # what the variant name itself says (trim + attributes)
    attrs: dict[str, str | None]  # what the row is: the name first, the list's columns second
    registration: str | None
    ev: bool


def row_words(row: dict[str, Any]) -> RowWords:
    model_name = str(row.get("model_name") or "")
    tokens = drop_model_runs(raw_tokens(row.get("variant_name")), [model_name])
    reading = read_words(tokens, model_name=model_name)
    attrs = dict(reading.attrs)
    columns = {
        "fuel": row.get("fuel_powertrain"), "transmission": row.get("transmission"),
        "drive": row.get("drive"), "seater": row.get("seater"),
    }
    for key, value in columns.items():
        if attrs[key] is None and value:
            text_value = str(value).strip().upper()
            if key == "fuel":
                attrs[key] = _FUEL.get(text_value, text_value)
            elif key == "transmission":
                attrs[key] = _TRANSMISSION.get(text_value, text_value)
            elif key == "drive":
                attrs[key] = _DRIVE.get(text_value, text_value)
            else:
                digits = re.sub(r"\D", "", text_value)
                attrs[key] = str(int(digits)) if digits else None
    ev = row_is_ev(row)
    if ev and attrs["fuel"] in (None, "UNSPECIFIED"):
        attrs["fuel"] = "ELECTRIC"
    return RowWords(row=row, words=reading.tokens(), attrs=attrs, registration=row_registration(row), ev=ev)


# ── which model the document names ────────────────────────────────────────────
@dataclass(frozen=True)
class ModelPick:
    model_names: tuple[str, ...]  # the list's model names (the named one and its generation siblings)
    consumed: int  # how many of the document's words spell the model
    via: str  # NAME / ALIAS


def pick_model(
    tokens: list[str], *, model_names: list[str], aliases: list[tuple[str, str]],
) -> ModelPick | None:
    """The list model the document's words start with: the longest of the
    list's own names or an alias whose model the list holds (or whose
    generation family it holds: "THAR" against an October list that only
    names "NEW THAR"). Generation siblings ("SCORPIO N" / "New SCORPIO N")
    come along: the words then have to tell them apart."""
    by_key: dict[str, list[str]] = {}
    for name in model_names:
        by_key.setdefault(normalized_key(name), []).append(name)
    by_generation: dict[str, list[str]] = {}
    for name in model_names:
        by_generation.setdefault(generation_key(name), []).append(name)

    def consumed_by(key: str) -> int:
        glued = ""
        for index, token in enumerate(tokens):
            glued += token.replace("+", "")
            if glued == key:
                return index + 1
            if not key.startswith(glued):
                return 0
        return 0

    best: tuple[int, int, list[str], str] | None = None  # (consumed, key length, names, via)
    for key, names in by_key.items():
        used = consumed_by(key)
        if used and (best is None or (used, len(key)) > best[:2]):
            best = (used, len(key), names, "NAME")
    for alias_text, canonical in aliases:
        alias_key = normalized_key(alias_text)
        if not alias_key:
            continue
        used = consumed_by(alias_key)
        if not used:
            continue
        canonical_key = normalized_key(canonical)
        # The alias's model under this book's own spelling: the same name,
        # the same generation family ("NEW SCORPIO N" -> October's
        # "SCORPIO-N"; "THAR" -> "NEW THAR"), or a book that still prints a
        # longer combined name ending in it (September's "THE BOSS-
        # BOLERO+NEO" for "BOLERO NEO" until the September book is re-loaded
        # on the split Bolero models).
        names = (
            by_key.get(canonical_key)
            or by_generation.get(generation_key(canonical))
            or ([n for k, ns in by_key.items() if len(canonical_key) >= 6 and k.endswith(canonical_key) for n in ns]
                or None)
        )
        if not names:
            continue
        if best is None or (used, len(alias_key)) > best[:2]:
            best = (used, len(alias_key), names, "ALIAS")
    if best is None:
        return None
    used, _, names, via = best
    family = {generation_key(n) for n in names}
    siblings = [n for n in model_names if generation_key(n) in family]
    ordered = list(dict.fromkeys([*names, *siblings]))
    return ModelPick(model_names=tuple(ordered), consumed=used, via=via)


# ── identification ───────────────────────────────────────────────────────────
@dataclass
class WordResult:
    """What the words of one document say.

    ``status``:
      IDENTIFIED            exactly one row; ``row`` is the word-identified SKU
      AMBIGUOUS             several rows fit the words (``candidates``)
      REGISTRATION_UNKNOWN  the words fit the Private and the Commercial row
      CATEGORY_UNCERTAIN    EV or ICE is not certain from the document
      CATEGORY_CONFLICT     the document says both (e.g. "EV" and "DIESEL")
      ATTRIBUTE_CONFLICT    the document states one attribute twice, differently
      UNRECOGNISED_WORD     a trim-like word the list does not know
      NO_MATCH              the model is named but no row fits the words
      NO_MODEL              no list model is named at all
      NO_TEXT               nothing to read
    """

    status: str
    row: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    model_rows: list[dict[str, Any]] = field(default_factory=list)
    category: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def identified(self) -> bool:
        return self.status == "IDENTIFIED" and self.row is not None


def _conflicts(doc: dict[str, str | None], row: dict[str, str | None]) -> bool:
    return any(doc[k] and row[k] and doc[k] != row[k] for k in _ATTRIBUTE_KEYS)


def _match_one_model(doc: Reading, sigs: list[RowWords]) -> tuple[list[RowWords], list[str]]:
    """The rows of one model whose distinguishing words are exactly the
    document's, in both directions:

    * every word the document prints that the model's rows use is in the
      row, and no attribute it states contradicts the row;
    * of the rows that pass, only those carrying no word the document does
      not print beyond what another passing row also needs: "1.7 LX" never
      reaches "1.7 LX CBC", "MX3 PETROL AT" never reaches "MX3 PRO PM AT",
      "AX7L DSL AT 7 STR" never reaches the AWD or DT rows -- while a word
      that tells the row from no other ("AC" on "2.0L VXi AC", the list's
      "ADAS" tag) need not be printed.

    Returns (rows, unknown trim-like words)."""
    vocabulary = set().union(*(s.words for s in sigs)) if sigs else set()
    vocabulary_trim = {w for w in vocabulary if ":" not in w}
    # A bare battery number reads as the list's battery word ("59" -> B59).
    trim = {f"B{w}" if w.isdigit() and f"B{w}" in vocabulary_trim else w for w in doc.trim}
    # "N10 (O)" is the list's "N10 OPT"; "EDGE OPT" is the list's "EDGE(O)".
    for word in list(trim):
        if word not in vocabulary_trim and word.endswith("O") and word[:-1] in vocabulary_trim \
                and "OPT" in vocabulary_trim:
            trim = (trim - {word}) | {word[:-1], "OPT"}
    if "OPT" in trim and "OPT" not in vocabulary_trim:
        glued = [w for w in trim if f"{w}O" in vocabulary_trim]
        if len(glued) == 1:
            trim = (trim - {"OPT", glued[0]}) | {f"{glued[0]}O"}
    known = trim & vocabulary_trim
    unknown = sorted(w for w in trim - vocabulary_trim if _TRIM_LIKE.match(w))
    if unknown:
        return [], unknown
    doc_tokens = frozenset(known) | frozenset(
        _ATTRIBUTE_PREFIX[k] + v for k, v in doc.attrs.items() if v
    )
    consistent = [s for s in sigs if not _conflicts(doc.attrs, s.attrs) and known <= s.words]
    extras = [s.words - doc_tokens for s in consistent]
    matched = [
        sig for sig, extra in zip(consistent, extras, strict=True)
        if not any(other < extra for other in extras)
    ]
    return matched, []


def _pick_by_identity(
    model_text: str | None, variant_text: str | None, *, groups: set[str], aliases: list[tuple[str, str]],
    identity: Callable[[str | None, str | None], str], successors: dict[str, str],
) -> tuple[str, str, str, str] | None:
    """The model the document names, by the price masters' own model
    identity (``oem_price_masters.price_list_model_key``: any month's
    spelling -- "XUV3XO" / "XUV 3XO", "NEW THAR" / "NEW THAR 2WD & 4WD",
    "SCORPIO-N" = the new Scorpio N, the Bolero family split by series).
    The longest run of the document's leading words that is a model of this
    book wins; then an alias of one; then, when the model the words name has
    left the book, its successor ("SCORPIO N" on an October date). Returns
    (identity, the words that name it, the rest, how) or None."""
    words = " ".join(str(v) for v in (model_text, variant_text) if v and str(v).strip()).split()
    alias_by_key = {normalized_key(a): c for a, c in aliases if normalized_key(a)}
    splits = [(" ".join(words[:i]), " ".join(words[i:]) or None) for i in range(len(words), 0, -1)]
    # The longest run of words first, so "SCORPIO N" never stops at
    # "SCORPIO" (the Classic) on a book that has no old Scorpio N.
    def without_repeat(prefix: str, rest: str | None) -> str | None:
        """The rest without the model repeated at its start (the series
        letter of a Bolero is read from the vehicle's own words)."""
        if not rest:
            return rest
        target, glued, rest_words = normalized_key(prefix), "", rest.split()
        for index, word in enumerate(rest_words):
            glued += normalized_key(word)
            if glued == target:
                return " ".join(rest_words[index + 1:]) or None
            if not target.startswith(glued):
                break
        return rest

    splits = [(prefix, without_repeat(prefix, rest)) for prefix, rest in splits]
    for prefix, rest in splits:
        key = identity(prefix, rest)
        if key in groups:
            return key, prefix, rest or "", "NAME"
        canonical = alias_by_key.get(normalized_key(prefix))
        alias_key = identity(canonical, rest) if canonical is not None else None
        if alias_key in groups:
            return alias_key, prefix, rest or "", "ALIAS"
        for named in (key, alias_key):
            if named and successors.get(named) in groups:
                return successors[named], prefix, rest or "", "SUCCESSOR"
    return None


def identify(
    *,
    rows: list[dict[str, Any]],
    model_text: str | None,
    variant_text: str | None,
    aliases: list[tuple[str, str]] | None = None,
    registration: str | None = None,
    gst_rate: Decimal | None = None,
    identity: Callable[[str | None, str | None], str] | None = None,
    successors: dict[str, str] | None = None,
) -> WordResult:
    """Name the SKU from one document's words. ``registration`` is PRIVATE /
    COMMERCIAL when known; ``gst_rate`` is the invoice's printed GST rate in
    percent (5 = EV, 12 and above = ICE), None for a booking form.

    ``identity`` (the price masters' model identity, 2026-10-20) groups the
    book's rows into models however a month spelt them and finds the model
    the document names under either spelling; without it the book's own
    model names and the aliases do, with generation siblings."""
    text_value = " ".join(str(v) for v in (model_text, variant_text) if v and str(v).strip())
    tokens = raw_tokens(text_value)
    if not tokens:
        return WordResult(status="NO_TEXT")

    sigs_by_model: dict[str, list[RowWords]] = {}
    strip_name: dict[str, str] = {}
    for row in rows:
        group = (identity(row.get("model_name"), row.get("variant_name")) if identity
                 else str(row.get("model_name") or ""))
        sigs_by_model.setdefault(group, []).append(row_words(row))
        strip_name.setdefault(group, str(row.get("model_name") or ""))

    if identity is not None:
        found = _pick_by_identity(model_text, variant_text, groups=set(sigs_by_model), aliases=aliases or [],
                                  identity=identity, successors=successors or {})
        if found is None:
            return WordResult(status="NO_MODEL", detail={"words": tokens})
        key, prefix, rest_text, via = found
        picked = [key]
        consumed = raw_tokens(prefix)
        spellings = [prefix, *{str(s.row.get("model_name") or "") for s in sigs_by_model[key]}]
        rest = drop_model_runs(raw_tokens(rest_text), spellings)
        model_label = strip_name[key]
    else:
        model_names = list(dict.fromkeys(str(r.get("model_name") or "") for r in rows if r.get("model_name")))
        pick = pick_model(tokens, model_names=model_names, aliases=aliases or [])
        if pick is None:
            return WordResult(status="NO_MODEL", detail={"words": tokens})
        picked = list(pick.model_names)  # ordered: the named model first, then its siblings
        consumed = tokens[:pick.consumed]
        rest = drop_model_runs(tokens[pick.consumed:], list(pick.model_names))
        via = pick.via
        model_label = pick.model_names[0]
    reading = read_words(rest, model_name=model_label)
    detail: dict[str, Any] = {
        "model": model_label, "models": [strip_name.get(p, p) for p in picked], "modelVia": via,
        "words": sorted(reading.trim), "stated": {k: v for k, v in reading.attrs.items() if v},
    }

    # Category first (R4): EV or ICE, from what the document says.
    picked_ev = {name: any(s.ev for s in sigs_by_model.get(name, [])) for name in picked}
    stated: set[str] = set()
    # EV words: the word itself (also inside the model the document names,
    # "XUV 3XO EV"), or a battery size (B59, B79, 39.4 kWh -> B39.4; never
    # Bolero's B4 / B6 / B8 trims).
    ev_signal = (
        reading.ev_word
        or any(t in {"EV", "ELECTRIC", "BEV"} for t in consumed)
        or any(re.fullmatch(r"B[3-9]\d(?:\.\d+)?", w) for w in reading.trim)
    )
    if ev_signal or (gst_rate is not None and gst_rate <= Decimal(5)):
        stated.add("EV")
    if reading.fuels & _ICE_FUELS or (gst_rate is not None and gst_rate >= Decimal(12)):
        stated.add("ICE")
    detail["categoryStated"] = sorted(stated)
    if reading.conflicts:
        detail["conflicts"] = sorted(reading.conflicts)
        return WordResult(status="ATTRIBUTE_CONFLICT", detail=detail,
                          model_rows=[s.row for n in picked for s in sigs_by_model.get(n, [])])
    if len(stated) == 2:
        return WordResult(status="CATEGORY_CONFLICT", detail=detail,
                          model_rows=[s.row for n in picked for s in sigs_by_model.get(n, [])])

    def counterpart_models(ev: bool) -> list[str]:
        """The same nameplate in the other category ("XUV3XO" <-> "XUV3XO EV")."""
        out = []
        for name in picked:
            base = generation_key(name)
            for other, sigs in sigs_by_model.items():
                if other in picked or not sigs:
                    continue
                other_key = generation_key(other)
                same = other_key == base + "EV" or base == other_key + "EV"
                if same and any(s.ev for s in sigs) == ev:
                    out.append(other)
        return list(dict.fromkeys(out))

    picked_is_ev = any(picked_ev.values())
    picked_is_ice = any(not v for v in picked_ev.values())
    if stated == {"EV"}:
        models = [n for n in picked if picked_ev[n]] or counterpart_models(ev=True)
        category = "EV"
    elif stated == {"ICE"}:
        models = [n for n in picked if not picked_ev[n]] or counterpart_models(ev=False)
        category = "ICE"
    else:
        other = counterpart_models(ev=not picked_is_ev) if picked_is_ev != picked_is_ice else []
        if other or (picked_is_ev and picked_is_ice):
            # "XUV 3XO" / "AX5" with no fuel: the list has it as petrol,
            # diesel and electric; the PC says which (never the price).
            pool = [s.row for n in [*picked, *other] for s in sigs_by_model.get(n, [])]
            return WordResult(status="CATEGORY_UNCERTAIN", detail=detail, model_rows=pool)
        models = list(picked)
        category = "EV" if picked_is_ev else "ICE"
    detail["category"] = category
    if not models:
        return WordResult(status="NO_MATCH", category=category, detail=detail)

    matched: list[RowWords] = []
    pool: list[dict[str, Any]] = []
    unknown: list[str] = []
    for name in models:
        sigs = sigs_by_model.get(name, [])
        if registration is not None:
            sigs = [s for s in sigs if s.registration in (None, registration)]
        pool.extend(s.row for s in sigs)
        found, unrecognised = _match_one_model(reading, sigs)
        unknown.extend(unrecognised)
        matched.extend(found)
    detail["matchedCount"] = len(matched)
    if unknown and not matched:
        detail["unrecognised"] = sorted(set(unknown))
        return WordResult(status="UNRECOGNISED_WORD", category=category, detail=detail, model_rows=pool)
    if not matched:
        return WordResult(status="NO_MATCH", category=category, detail=detail, model_rows=pool)
    rows_matched = [s.row for s in matched]
    if len(matched) == 1:
        sole_row_models = all(len(sigs_by_model.get(n, [])) == 1 for n in models)
        if not reading.trim and not sole_row_models:
            # The model alone, no trim word: never a word match.
            return WordResult(status="AMBIGUOUS", category=category, candidates=rows_matched,
                              detail=detail, model_rows=pool)
        return WordResult(status="IDENTIFIED", row=matched[0].row, candidates=rows_matched,
                          category=category, detail=detail, model_rows=pool)
    if len(matched) == 2 and {m.registration for m in matched} == {"PRIVATE", "COMMERCIAL"} \
            and matched[0].words == matched[1].words:
        return WordResult(status="REGISTRATION_UNKNOWN", category=category, candidates=rows_matched,
                          detail=detail, model_rows=pool)
    return WordResult(status="AMBIGUOUS", category=category, candidates=rows_matched, detail=detail,
                      model_rows=pool)


# ── price: a check on a word-identified SKU, and the order of a shortlist ────
def price_check(master: Decimal | None, offered: Decimal | None) -> str:
    """MATCH / DIFFERS / MISSING: the SKU's master ex-showroom against the
    document's own (both GST-inclusive), within ``PRICE_TOLERANCE``."""
    if master is None or offered is None:
        return "MISSING"
    return "MATCH" if abs(Decimal(master) - Decimal(offered)) <= PRICE_TOLERANCE else "DIFFERS"


def order_shortlist(
    rows: list[dict[str, Any]], *, model_text: str | None, variant_text: str | None,
    offered_ex_showroom: Decimal | None, limit: int = 10,
) -> list[dict[str, Any]]:
    """Order a shortlist for the PC: most shared words first, then the
    nearest master ex-showroom to the document's, then the closest spelling
    of the trim. Ordering only -- nothing here is ever pinned."""
    reading = read_words(raw_tokens(" ".join(str(v) for v in (model_text, variant_text) if v)), model_name=None)
    doc = reading.tokens()

    def key(row: dict[str, Any]) -> tuple[int, Decimal, float]:
        words = row_words(row).words
        shared = len(words & doc)
        # A misread trim ("Z9S") is closer to "Z8S" than to "Z8T"; the
        # model's own words are not trim.
        model_words = _model_words(row.get("model_name"))
        doc_trim = "".join(sorted(w for w in reading.trim if w.replace("+", "") not in model_words))
        row_trim = "".join(sorted(w for w in words if ":" not in w))
        similar = SequenceMatcher(None, doc_trim, row_trim).ratio() if doc_trim and row_trim else 0.0
        master = row.get("master_ex_showroom")
        gap = (abs(Decimal(str(master)) - offered_ex_showroom)
               if master is not None and offered_ex_showroom is not None else Decimal("1e12"))
        return (-shared, gap, -similar)

    unique = list({id(r): r for r in rows}.values())
    return sorted(unique, key=key)[:limit]


__all__ = [
    "PRICE_TOLERANCE",
    "ModelPick",
    "Reading",
    "RowWords",
    "WordResult",
    "clear_top",
    "drop_model_runs",
    "generation_key",
    "identify",
    "normalized_key",
    "order_shortlist",
    "pick_model",
    "price_check",
    "raw_tokens",
    "read_words",
    "row_is_ev",
    "row_registration",
    "row_words",
]


def clear_top(rows: list[dict[str, Any]], *, model_text: str | None, variant_text: str | None) -> bool:
    """Whether the first row of a shortlist shares more of the document's
    words than the second -- the only case a one-click suggestion is
    offered; a tie on words is never broken for the PC by price (review
    #471 S8)."""
    if len(rows) < 2:
        return bool(rows)
    doc = read_words(raw_tokens(" ".join(str(v) for v in (model_text, variant_text) if v)), model_name=None).tokens()
    shared = [len(row_words(r).words & doc) for r in rows[:2]]
    return shared[0] > shared[1]
