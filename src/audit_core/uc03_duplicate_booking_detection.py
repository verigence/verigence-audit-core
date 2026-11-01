"""uc03_duplicate_booking_detection.py — the same real customer must not
appear as two separate, unrelated Bookings within one tenant.

Native audit-core rule, not the rule-engine's own CROSS_CASE mechanism
(DUPLICATE_PAN_ACROSS_BOOKINGS etc.) -- that mechanism is parked
(rule-engine migration 0004): it has no existing cross-journey finding
materialization pattern (a finding spans exactly one journey via a
required composite FK), and audit-core has no caller for it at all. This
module builds the check natively where the materialization model, the
customer-identity fuzzy-match logic (already proven in
uc03_customer_identity_consistency.py), and the "which one is original"
signal (BK_MIN_BOOKING_AMOUNT_NOT_MET, also audit-core's own) all already
live in one place.

Match signals, checked in order from strongest to weakest -- a pair only
ever gets ONE finding, on the strongest basis that actually matches, not
one per basis (stacking near-duplicate flags for the same relationship is
noise, not signal):

  - exact PAN number match                                   -- Strong
  - exact Aadhaar number match                                -- Strong
  - this journey's customer name matches the OTHER journey's KYC-recorded
    relationship name (Father/Spouse/Husband), or vice versa  -- Strong
  - fuzzy KYC name match (same surname, and a given name close enough to
    be an OCR/spelling variant of the same person -- see
    _same_person_by_name) AND same address pincode -- name alone is too
    weak a signal on its own (common names collide); pincode alone says
    nothing about identity                                    -- Strong
  - exact buyer GSTIN match (from the Tax Invoice, when either journey
    has one on file)                                          -- Moderate
  - exact normalized mobile number match                      -- Moderate
  - same surname (last token of the KYC name) AND same address pincode --
    weaker than the full-name-and-pincode match above, catches the case
    where two different-but-related people share a surname and an
    address                                                   -- Weak
  - fuzzy full-address match (SequenceMatcher, same technique as name
    matching, lower threshold -- addresses are long and OCR-noisy) without
    requiring an exact pincode                                -- Weak

Given a matched pair, whichever journey actually reached its minimum
booking amount EARLIER holds the booking -- not just "confirmed or not":
``journey_stage_states.booking_confirm_date`` (set by
``evaluate_minimum_booking_payment`` to the receipt date on which the
running total first met the minimum, not the date it happened to be
evaluated) is the real-world "who paid first" signal, compared directly
between the two journeys. If only one side has reached its minimum at
all, that one holds it outright. If neither has, the earlier-CREATED
journey is the fallback -- timing of intent is weaker evidence than
timing of an actual payment commitment, but it's what's left when neither
side has paid anything toward the minimum yet. The finding is raised on
the journey that does NOT hold the booking, naming which journey it is
believed to duplicate.

``sync_duplicate_booking_detection`` is the producer: idempotent, self-heals
(a pairing that no longer matches resolves the finding), never raises.
"""
from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_customer_identity_consistency import _normalize_name
from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_manual_verification import _resolve_finding

logger = logging.getLogger(__name__)

_FINDING_TYPE = "DUPLICATE_BOOKING"
_RULE_PREFIX = "DUPLICATE_BOOKING"
_STAGE = "BOOKING"

_KYC_DOCUMENT_TYPES: tuple[str, ...] = ("aadhaar", "pan_card", "customer_kyc")
_NAME_FIELDS: dict[str, str] = {
    "aadhaar": "aadhaar_name",
    "pan_card": "pan_name",
    "customer_kyc": "customer_name",
}
# Preferred to weaker, matching the KYC precedence _NAME_FIELDS already uses.
_RELATIONSHIP_NAME_FIELDS: tuple[str, ...] = ("aadhaar_relationship_name", "pan_relationship_name")
_ADDRESS_FIELDS: tuple[str, ...] = ("aadhaar_address", "customer_address")

# Deciding "is this the same specific person" (surname already matched) is
# a stricter question than _names_match's own 0.55 (whether a document's
# stated name plausibly refers to a known reference name -- a different,
# more tolerant job in uc03_customer_identity_consistency.py). A whole-
# string ratio can't tell "Sanjay" vs "Sanjaya" (same person, OCR variant)
# apart from "Anil Sharma" vs "Sunil Sharma" (two different, very common
# names sharing a surname) -- short given names sharing a surname often
# score just as high or higher. Comparing the given-name portion alone,
# with its own tight threshold, is what actually separates the two.
_GIVEN_NAME_MATCH_THRESHOLD = 0.8
_NON_DIGIT = re.compile(r"\D+")
_WHITESPACE_ONLY = re.compile(r"\s+")

# Strongest to weakest -- _match_basis returns the first of these that
# actually matches, so a pair is never flagged more than once.
# Product owner 2026-10-28: the same PAN, Aadhaar, mobile or buyer GSTIN is a
# high-probability duplicate; the relationship name on one booking matching the customer name
# on another, or the same surname with the same pincode, is a probable case --
# a common name collides between unrelated people (SUKANTI SAHOO, W/O
# Ghanashyam Sahoo of Khordha, paired with GHANASYAM SAHOO, S/O Umakant Sahoo
# of Bhadrak, at "90%"). A merely similar address is not one of the rules and
# no longer pairs two bookings.
_SEVERITY_BY_BASIS: dict[str, str] = {
    "PAN": "CRITICAL",
    "AADHAAR": "CRITICAL",
    "CUSTOMER_MATCHES_RELATIVE": "MEDIUM",
    "NAME_AND_ADDRESS": "MEDIUM",
    "GST": "CRITICAL",
    "MOBILE": "CRITICAL",
    "SURNAME_AND_PINCODE": "MEDIUM",
    "SIMILAR_ADDRESS": "LOW",  # no longer raised; kept to read old findings
}

# A reviewer's real question isn't just "which basis matched" but "how
# likely is this to actually be the same person" -- a deterministic
# confidence estimate per basis, not a model score (there's no training
# data for this), reflecting how rarely each signal collides by chance:
# PAN/Aadhaar are near-unique identifiers; a relative's name repeated as
# another booking's own customer name is a very specific coincidence;
# mobile/GST can legitimately be shared within a household or business;
# surname+pincode or a merely-similar address are the weakest signals,
# common enough to collide between unrelated people.
_CONFIDENCE_PERCENT_BY_BASIS: dict[str, int] = {
    "PAN": 99,
    "AADHAAR": 99,
    "CUSTOMER_MATCHES_RELATIVE": 60,
    "NAME_AND_ADDRESS": 60,
    "GST": 95,
    "MOBILE": 95,
    "SURNAME_AND_PINCODE": 50,
    "SIMILAR_ADDRESS": 35,
}
# One-to-one with _SEVERITY_BY_BASIS's own tiers -- kept as an explicit,
# separate mapping (not derived inline) so a reviewer-facing label never
# silently drifts if the internal severity tiers used for SLA routing
# ever change independently of this.
_CONFIDENCE_LABEL_BY_SEVERITY: dict[str, str] = {
    "CRITICAL": "HIGH",
    "MEDIUM": "PROBABLE",
    "LOW": "LOW",
}

_BASIS_LABEL: dict[str, str] = {
    "PAN": "PAN",
    "AADHAAR": "Aadhaar",
    "CUSTOMER_MATCHES_RELATIVE": "customer name matches a relative on the other booking",
    "NAME_AND_ADDRESS": "name and address",
    "GST": "GSTIN",
    "MOBILE": "mobile number",
    "SURNAME_AND_PINCODE": "surname and pincode",
    "SIMILAR_ADDRESS": "similar address",
}

_IDENTITY_FIELD_KEYS: list[str] = [
    "pan_number", "aadhaar_number", "address_pincode", "customer_phone",
    *_ADDRESS_FIELDS, *_RELATIONSHIP_NAME_FIELDS, *_NAME_FIELDS.values(),
]


# ── what counts as an identifier at all (2026-10-23) ──────────────────────────
# Every match basis below compares one journey's extracted value with
# another's. Until now any non-empty string counted, so two journeys whose
# Tax Invoice carried "No" (or "NA", or a bare number) in the buyer GSTIN
# field matched each other on GSTIN at 75% confidence -- an individual buying
# a Thar paired with an LLP, because neither has a GSTIN and the extractor
# wrote the same placeholder on both. The same held for PAN, Aadhaar, pincode
# and address: a shared placeholder is the ABSENCE of an identifier, and two
# absences are not a match.
#
# So a value is now only usable as a signal when it is the right SHAPE for
# what it claims to be. A wrongly-shaped value is dropped (the journey simply
# has no value for that field) rather than compared.
_PLACEHOLDER_TOKENS: frozenset[str] = frozenset({
    "", "NA", "NAN", "NIL", "NO", "NONE", "NOTAVAILABLE", "NOTAPPLICABLE",
    "NOTFOUND", "NOTMENTIONED", "NOTPROVIDED", "NOTGIVEN", "UNKNOWN", "UNAVAILABLE",
    "NULL", "BLANK", "EMPTY", "YES", "TEST", "DUMMY", "SAMPLE", "ABSENT",
})
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
# 15 chars: 2-digit state code, 10-char PAN, 1 entity digit/letter, 'Z', 1 check char.
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
# 5 letters, 4 digits, 1 letter. The 4th letter is the holder type and the 5th
# the surname initial; neither is checked, because an OCR slip there would make
# the value differ from the other side's anyway -- the shape is what rules out
# "NA" and bare numbers.
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_DIGITS_ONLY = re.compile(r"^[0-9]+$")
# Indian state codes actually issued: 01-38, plus 97 (other territory) and 99 (centre).
_GSTIN_STATE_CODES: frozenset[str] = frozenset(
    [f"{n:02d}" for n in range(1, 39)] + ["97", "99"]
)


def _placeholder(value: str | None) -> bool:
    """True for a value that states the field has no value -- "NA", "No",
    "-", "0000", "XXXX" and the like, in any spacing or punctuation."""
    if value is None:
        return True
    squashed = _NON_ALNUM.sub("", value.upper())
    if not squashed:
        return True
    if squashed in _PLACEHOLDER_TOKENS:
        return True
    # One character repeated ("0000", "XXXXXXXX", "----") says nothing.
    return len(set(squashed)) == 1


def _valid_gstin(value: Any) -> str | None:
    """A GSTIN in its documented shape, or None. Nothing else is a GSTIN --
    least of all "No" on an invoice to a customer who has none."""
    if value is None:
        return None
    candidate = _NON_ALNUM.sub("", str(value).upper())
    if _placeholder(candidate) or not _GSTIN_RE.match(candidate):
        return None
    if candidate[:2] not in _GSTIN_STATE_CODES:
        return None
    return candidate


def _valid_pan(value: Any) -> str | None:
    if value is None:
        return None
    candidate = _NON_ALNUM.sub("", str(value).upper())
    if _placeholder(candidate) or not _PAN_RE.match(candidate):
        return None
    return candidate


def _valid_aadhaar(value: Any) -> str | None:
    """12 digits, not starting 0 or 1 (UIDAI issues none), and not a single
    repeated digit or a plain 1..9 run."""
    if value is None:
        return None
    candidate = _NON_DIGIT.sub("", str(value))
    if len(candidate) != 12 or candidate[0] in "01":
        return None
    if len(set(candidate)) == 1 or candidate in ("123456789012", "098765432109"):
        return None
    return candidate


def _valid_pincode(value: Any) -> str | None:
    """6 digits, first digit 1-8 (the postal circles actually in use)."""
    if value is None:
        return None
    candidate = _NON_DIGIT.sub("", str(value))
    if len(candidate) != 6 or candidate[0] not in "12345678":
        return None
    if len(set(candidate)) == 1:
        return None
    return candidate


def _meaningful_text(value: Any, *, minimum_length: int) -> str | None:
    """A name or address with enough substance to compare: not a
    placeholder, and long enough that a fuzzy ratio means something. "NA"
    equals "NA" exactly, which used to raise a SIMILAR_ADDRESS pair."""
    if value is None:
        return None
    text_value = str(value).strip()
    if _placeholder(text_value):
        return None
    letters = sum(1 for character in text_value if character.isalpha())
    if letters < 2 or len(_NON_ALNUM.sub("", text_value.upper())) < minimum_length:
        return None
    return text_value


_MINIMUM_NAME_LENGTH = 4
_MINIMUM_ADDRESS_LENGTH = 12


def _normalize_mobile(value: Any) -> str | None:
    """The last 10 digits, when they form a real Indian mobile number.
    A landline-length string, a repeated digit or a number beginning 0-5 is
    not a mobile and must not pair two journeys together."""
    digits = _NON_DIGIT.sub("", str(value or ""))
    if len(digits) < 10:
        return None
    candidate = digits[-10:]  # drops a country code / leading trunk prefix
    if candidate[0] not in "6789" or len(set(candidate)) == 1:
        return None
    return candidate


def _surname(name: str | None) -> str | None:
    if not name:
        return None
    tokens = _normalize_name(name).split()
    return tokens[-1] if tokens else None


def _name_parts(name: str | None) -> tuple[str, str] | None:
    """(given-name portion, surname) -- surname is the last token, given
    name is everything before it. None when there's only one token (a bare
    surname on its own establishes nothing about whether it's the same
    person, only that _surname() itself matched)."""
    if not name:
        return None
    tokens = _normalize_name(name).split()
    if len(tokens) < 2:
        return None
    return " ".join(tokens[:-1]), tokens[-1]


def _same_person_by_name(a: str | None, b: str | None) -> bool:
    """True when two names plausibly name the same specific person: same
    surname, and a given-name portion close enough to be an OCR/spelling
    variant rather than a different person who happens to share a
    surname. See _GIVEN_NAME_MATCH_THRESHOLD for why a tight threshold on
    the given name alone, not _names_match's whole-string ratio."""
    parts_a, parts_b = _name_parts(a), _name_parts(b)
    if not parts_a or not parts_b:
        return False
    given_a, surname_a = parts_a
    given_b, surname_b = parts_b
    if surname_a != surname_b:
        return False
    if given_a == given_b:
        return True
    return SequenceMatcher(None, given_a, given_b).ratio() >= _GIVEN_NAME_MATCH_THRESHOLD


def _extract_signals(rows: list[Any], *, gst: str | None) -> dict[str, Any] | None:
    """Shared shape-building for both this journey's own signals and a
    candidate's: pick the highest-confidence value per field, then merge
    into one flat, comparison-ready record. gst is looked up separately
    (invoice_review_values, not journey_document_extracted_fields) and
    passed in rather than derived here -- a journey whose only identifying
    signal is its buyer GSTIN (no KYC extraction at all yet) must still
    count as having something to compare, not be silently dropped."""
    by_field = {r["field_key"]: str(r["effective_value"] or "").strip() for r in rows}
    # Every value is validated for its own shape before it can pair two
    # journeys; a placeholder or wrongly-shaped value counts as absent
    # (2026-10-23 -- see the validators above).
    name = None
    for kyc_type in _KYC_DOCUMENT_TYPES:
        field_key = _NAME_FIELDS.get(kyc_type)
        if field_key:
            candidate_name = _meaningful_text(by_field.get(field_key), minimum_length=_MINIMUM_NAME_LENGTH)
            if candidate_name:
                name = candidate_name
                break
    relative_name = next(
        (value for value in (
            _meaningful_text(by_field.get(k), minimum_length=_MINIMUM_NAME_LENGTH)
            for k in _RELATIONSHIP_NAME_FIELDS) if value),
        None,
    )
    address = next(
        (value for value in (
            _meaningful_text(by_field.get(k), minimum_length=_MINIMUM_ADDRESS_LENGTH)
            for k in _ADDRESS_FIELDS) if value),
        None,
    )

    pan = _valid_pan(by_field.get("pan_number"))
    aadhaar = _valid_aadhaar(by_field.get("aadhaar_number"))
    pincode = _valid_pincode(by_field.get("address_pincode"))
    mobile = _normalize_mobile(by_field.get("customer_phone"))
    if not (pan or aadhaar or name or mobile or address or gst):
        return None
    return {
        "pan": pan, "aadhaar": aadhaar, "name": name, "pincode": pincode,
        "mobile": mobile, "address": address, "relative_name": relative_name,
        "surname": _surname(name), "gst": gst,
    }


# A document of another customer's file, taken out of its booking
# (uc03_p2_file_identity), is not the booking's: its PAN, Aadhaar or mobile
# never pairs that booking with the customer's own booking (2026-11-01: 25
# moved files' new bookings were paired with the booking they were taken out
# of). Out means its evidence is FOREIGN, or it was read from a file taken
# out (identity_enforced_at_utc) -- a copy of it voided earlier, when its
# pages were grouped again, still names the same customer.
_NOT_TAKEN_OUT = """NOT EXISTS (
                      SELECT 1 FROM auditcore.evidence fe
                      WHERE fe.tenant_id = f.tenant_id AND fe.journey_id = f.journey_id
                        AND fe.di_document_id = f.di_document_id AND fe.association_status = 'FOREIGN')
                  AND NOT EXISTS (
                      SELECT 1 FROM auditcore.p2_document_queue fq
                      JOIN auditcore.p2_upload_batches fb
                        ON fb.tenant_id = fq.tenant_id AND fb.batch_id = fq.batch_id
                      WHERE fq.tenant_id = f.tenant_id AND fq.journey_id = f.journey_id
                        AND fq.di_document_id = f.di_document_id AND fb.identity_enforced_at_utc IS NOT NULL)"""


def _identity_signals(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any] | None:
    """This journey's own PAN / Aadhaar / KYC name / mobile / address /
    relationship name / pincode / buyer GSTIN, each the latest-confidence
    extracted value. None when nothing identifying has been captured yet
    -- nothing to compare other journeys against."""
    rows = connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT f.field_key, f.effective_value,
                       ROW_NUMBER() OVER (
                           PARTITION BY f.field_key
                           ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
                       ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id = :tenant_id AND f.journey_id = :journey_id
                  AND f.field_key = ANY(:field_keys)
                  AND f.effective_value IS NOT NULL
                  AND """ + _NOT_TAKEN_OUT + """
            )
            SELECT field_key, effective_value FROM ranked WHERE row_rank = 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "field_keys": _IDENTITY_FIELD_KEYS},
    ).mappings().all()
    gst = _valid_gstin(_buyer_gstin(connection, tenant_id=tenant_id, journey_id=journey_id))
    return _extract_signals(rows, gst=gst)


# The customer's GSTIN (product owner 2026-10-28): the buyer GSTIN of a live
# customer invoice only. A wholesale invoice's buyer is the dealership, and a
# GSTIN carrying a dealer's own PAN (printed as seller / dealer GSTIN anywhere in
# the Project) is the dealership's -- Utkal Automobiles' 21AAACU2665C1ZV paired
# AMIT PALEI with Subrat Kumar Mallick.
_BUYER_GSTIN_SQL = """
    SELECT v.journey_id, v.buyer_gstin, v.created_at_utc
    FROM auditcore.invoice_review_values v
    JOIN auditcore.evidence e
      ON e.tenant_id = v.tenant_id AND e.journey_id = v.journey_id
     AND e.di_document_id = v.source_di_document_id AND e.association_status = 'ACTIVE'
    WHERE v.tenant_id = :tenant_id AND v.buyer_gstin IS NOT NULL
      AND COALESCE(v.document_type_key, '') <> 'wholesale_invoice'
      AND substr(upper(regexp_replace(v.buyer_gstin, '\\s', '', 'g')), 3, 10) NOT IN (
          SELECT substr(upper(regexp_replace(f.effective_value #>> '{}', '\\s', '', 'g')), 3, 10)
          FROM auditcore.journey_document_extracted_fields f
          WHERE f.tenant_id = :tenant_id AND f.field_key IN ('seller_gstin', 'dealer_gstin')
            AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
          UNION
          SELECT substr(upper(regexp_replace(s.seller_gstin, '\\s', '', 'g')), 3, 10)
          FROM auditcore.invoice_review_values s
          WHERE s.tenant_id = :tenant_id AND s.seller_gstin IS NOT NULL
      )
"""

def _buyer_gstin(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str | None:
    """The latest non-null buyer GSTIN off any Tax Invoice materialized for
    this journey -- lives in invoice_review_values, a separate table from
    journey_document_extracted_fields (see uc03_invoice_materialization.py)."""
    return connection.execute(
        text(
            """
            SELECT g.buyer_gstin FROM (""" + _BUYER_GSTIN_SQL + """) g
            WHERE g.journey_id = :journey_id
            ORDER BY g.created_at_utc DESC LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()


def _candidate_journeys(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> list[dict[str, Any]]:
    """Every other journey in this tenant, with its own latest identity
    signals -- one row per journey, aggregated in SQL so this stays one
    round trip regardless of tenant size."""
    rows = connection.execute(
        text(
            """
            WITH ranked AS (
                SELECT f.journey_id, f.field_key, f.effective_value,
                       ROW_NUMBER() OVER (
                           PARTITION BY f.journey_id, f.field_key
                           ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
                       ) AS row_rank
                FROM auditcore.journey_document_extracted_fields f
                JOIN auditcore.journeys j
                  ON j.tenant_id = f.tenant_id AND j.journey_id = f.journey_id
                WHERE f.tenant_id = :tenant_id AND f.journey_id <> :journey_id
                  AND f.field_key = ANY(:field_keys)
                  AND f.effective_value IS NOT NULL
                  AND """ + _NOT_TAKEN_OUT + """
            )
            SELECT r.journey_id, r.field_key, r.effective_value, j.created_at_utc
            FROM ranked r
            JOIN auditcore.journeys j
              ON j.tenant_id = :tenant_id AND j.journey_id = r.journey_id
            WHERE r.row_rank = 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "field_keys": _IDENTITY_FIELD_KEYS},
    ).mappings().all()

    by_journey: dict[UUID, list[Any]] = {}
    created_at_by_journey: dict[UUID, Any] = {}
    for row in rows:
        by_journey.setdefault(row["journey_id"], []).append(row)
        created_at_by_journey[row["journey_id"]] = row["created_at_utc"]

    gst_rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (v.journey_id) v.journey_id, v.buyer_gstin, j.created_at_utc
            FROM (""" + _BUYER_GSTIN_SQL + """) v
            JOIN auditcore.journeys j ON j.tenant_id = :tenant_id AND j.journey_id = v.journey_id
            WHERE v.journey_id <> :journey_id
            ORDER BY v.journey_id, v.created_at_utc DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    gst_by_journey = {
        row["journey_id"]: value
        for row in gst_rows
        if (value := _valid_gstin(row["buyer_gstin"])) is not None
    }
    for row in gst_rows:
        created_at_by_journey.setdefault(row["journey_id"], row["created_at_utc"])

    candidates = []
    # A journey whose only identifying signal is its buyer GSTIN (no KYC
    # extraction at all yet) has no entry in by_journey -- iterate the
    # union of both sources, or GST-only candidates are silently dropped
    # before ever being compared (the exact bug this fixes).
    for candidate_journey_id in by_journey.keys() | gst_by_journey.keys():
        journey_rows = by_journey.get(candidate_journey_id, [])
        signals = _extract_signals(journey_rows, gst=gst_by_journey.get(candidate_journey_id))
        if signals is None:
            continue
        signals["journey_id"] = candidate_journey_id
        signals["created_at_utc"] = created_at_by_journey[candidate_journey_id]
        candidates.append(signals)
    return candidates


def _match_basis(signals: dict[str, Any], candidate: dict[str, Any]) -> str | None:
    if signals["pan"] and candidate["pan"] and signals["pan"] == candidate["pan"]:
        return "PAN"
    if signals["aadhaar"] and candidate["aadhaar"] and signals["aadhaar"] == candidate["aadhaar"]:
        return "AADHAAR"
    if _same_person_by_name(signals["name"], candidate["relative_name"]) or _same_person_by_name(
        candidate["name"], signals["relative_name"]
    ):
        return "CUSTOMER_MATCHES_RELATIVE"
    if (
        signals["pincode"] and candidate["pincode"] and signals["pincode"] == candidate["pincode"]
        and _same_person_by_name(signals["name"], candidate["name"])
    ):
        return "NAME_AND_ADDRESS"
    if signals["gst"] and candidate["gst"] and signals["gst"] == candidate["gst"]:
        return "GST"
    if signals["mobile"] and candidate["mobile"] and signals["mobile"] == candidate["mobile"]:
        return "MOBILE"
    if (
        signals["surname"] and candidate["surname"] and signals["surname"] == candidate["surname"]
        and signals["pincode"] and candidate["pincode"] and signals["pincode"] == candidate["pincode"]
    ):
        return "SURNAME_AND_PINCODE"
    return None


# ── what actually matched, for the reviewer (2026-10-23) ─────────────────────
# "GSTIN · 75%" tells a reviewer the basis but not the evidence, so a wrong
# pairing could not be told from a right one without opening both journeys.
# The values that matched are recorded on the finding when it is raised and
# shown beside the pair. Numbers that identify a person are masked to their
# last four; a GSTIN and a pincode are not, because they identify a business
# or a locality and are what the reviewer has to read to judge the match.
_MASKED_FIELDS: frozenset[str] = frozenset({"pan", "aadhaar", "mobile"})
_MATCH_FIELD_LABEL: dict[str, str] = {
    "pan": "PAN",
    "aadhaar": "Aadhaar",
    "gst": "Buyer GSTIN",
    "mobile": "Mobile",
    "name": "KYC name",
    "relative_name": "Relative on KYC",
    "surname": "Surname",
    "pincode": "Pincode",
    "address": "Address",
}
# Which signals each basis actually compared, in the order they were compared.
_BASIS_FIELDS: dict[str, tuple[str, ...]] = {
    "PAN": ("pan",),
    "AADHAAR": ("aadhaar",),
    "CUSTOMER_MATCHES_RELATIVE": ("name", "relative_name"),
    "NAME_AND_ADDRESS": ("name", "pincode"),
    "GST": ("gst",),
    "MOBILE": ("mobile",),
    "SURNAME_AND_PINCODE": ("surname", "pincode"),
    "SIMILAR_ADDRESS": ("address",),
}
_MAX_SHOWN_LENGTH = 80


def _shown(field: str, value: Any) -> str | None:
    """One value as a reviewer should see it: masked where it identifies a
    person, trimmed where it is long."""
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value:
        return None
    if field in _MASKED_FIELDS:
        return text_value if len(text_value) <= 4 else f"{'X' * (len(text_value) - 4)}{text_value[-4:]}"
    if len(text_value) > _MAX_SHOWN_LENGTH:
        return f"{text_value[:_MAX_SHOWN_LENGTH].rstrip()}…"
    return text_value


def _matched_fields(basis: str, signals: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """The field-by-field evidence behind one pairing: what this booking
    carried, what the other one carried, and whether the two are identical
    or merely similar (the fuzzy bases never compare equal strings)."""
    fields: list[dict[str, Any]] = []
    for field in _BASIS_FIELDS.get(basis, ()):
        if basis == "CUSTOMER_MATCHES_RELATIVE":
            # The comparison crosses the two sides: one booking's customer
            # against the other's named relative, whichever way round matched.
            this_value, other_value = (
                (signals["name"], candidate["relative_name"])
                if _same_person_by_name(signals["name"], candidate["relative_name"])
                else (signals["relative_name"], candidate["name"])
            )
            if field == "relative_name":
                continue
            fields.append({
                "field": "name_vs_relative",
                "label": "KYC name vs relative on the other booking",
                "thisValue": _shown("name", this_value),
                "otherValue": _shown("name", other_value),
                "exact": False,
            })
            continue
        this_value, other_value = signals.get(field), candidate.get(field)
        if this_value is None and other_value is None:
            continue
        fields.append({
            "field": field,
            "label": _MATCH_FIELD_LABEL.get(field, field),
            "thisValue": _shown(field, this_value),
            "otherValue": _shown(field, other_value),
            "exact": bool(this_value) and str(this_value) == str(other_value),
        })
    return fields


def _booking_confirm_date(connection: Connection, *, tenant_id: str, journey_id: UUID) -> Any:
    """The receipt date on which this journey's running Booking payments
    first met the minimum booking amount -- set by
    ``evaluate_minimum_booking_payment`` onto
    ``journey_stage_states.booking_confirm_date``. None when the minimum
    hasn't been reached yet (or no Booking stage row exists at all)."""
    return connection.execute(
        text(
            """
            SELECT booking_confirm_date FROM auditcore.journey_stage_states
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id AND stage_code = 'BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()


def _void_open_finding_with_stale_basis(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    rule_key: str,
    basis: str,
    correlation_id: str,
) -> bool:
    """Replace an OPEN finding that was raised on a different match basis.

    The basis, confidence and severity are fixed when a finding is raised
    (its RAISED event is append-only), and ``_machine_flag`` returns an
    existing finding untouched. A pair first seen on the mobile number alone
    therefore stayed "MOBILE 70%" after the PAN and Aadhaar were read and
    matched too. When the current basis differs, the OPEN finding is voided
    (closing its open tasks, as any void does) and ``_machine_flag`` then
    raises a fresh one on the current basis. A finding a Team Lead has
    already acknowledged is left alone: someone has reviewed it. A failure
    here leaves the finding as it was."""
    try:
        with connection.begin_nested():
            row = connection.execute(
                text(
                    """
                    SELECT f.audit_finding_id,
                           (SELECT e.safe_payload ->> 'matchBasis'
                              FROM auditcore.audit_finding_events e
                             WHERE e.tenant_id = f.tenant_id
                               AND e.audit_finding_id = f.audit_finding_id
                               AND e.event_type = 'RAISED'
                             ORDER BY e.occurred_at_utc LIMIT 1) AS stored_basis,
                           f.severity AS stored_severity
                    FROM auditcore.audit_findings f
                    WHERE f.tenant_id = :tenant_id AND f.journey_id = :journey_id
                      AND f.stage_code = :stage AND f.rule_key = :rule_key
                      AND f.finding_status = 'OPEN'
                    ORDER BY f.created_at_utc DESC, f.audit_finding_id DESC
                    LIMIT 1
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id, "stage": _STAGE, "rule_key": rule_key},
            ).mappings().one_or_none()
            if row is None or (row["stored_basis"] == basis
                                and row["stored_severity"] == _SEVERITY_BY_BASIS[basis]):
                return False
            note = (f"Match basis changed from {row['stored_basis'] or 'unknown'} to {basis}; "
                    "raised again on the current basis." if row["stored_basis"] != basis else
                    f"Match level for {basis} changed (2026-10-28); raised again at the current level.")
            voided = connection.execute(
                text(
                    """
                    UPDATE auditcore.audit_findings
                    SET finding_status = 'VOIDED', resolution_reason = :note,
                        updated_at_utc = now(), version_no = version_no + 1
                    WHERE tenant_id = :tenant_id AND audit_finding_id = :finding_id
                      AND finding_status = 'OPEN'
                    """
                ),
                {"tenant_id": tenant_id, "finding_id": row["audit_finding_id"], "note": note},
            )
            if voided.rowcount != 1:
                return False
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.audit_finding_events (
                        tenant_id, audit_finding_id, journey_id, stage_code,
                        event_type, actor_id, actor_role_snapshot, safe_payload, correlation_id
                    ) VALUES (
                        :tenant_id, :finding_id, :journey_id, :stage,
                        'VOIDED', NULL, 'SYSTEM', CAST(:payload AS jsonb), :correlation_id
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "finding_id": row["audit_finding_id"],
                    "journey_id": journey_id,
                    "stage": _STAGE,
                    "payload": json.dumps({"note": note, "previousBasis": row["stored_basis"], "currentBasis": basis}),
                    "correlation_id": correlation_id,
                },
            )
            return True
    except Exception:
        logger.warning("duplicate booking finding refresh failed", exc_info=True)
        return False


def _resolve_reverse_finding(
    connection: Connection, *, tenant_id: str, holder_journey_id: UUID, duplicate_journey_id: UUID,
    correlation_id: str,
) -> int:
    rows = connection.execute(
        text(
            """
            SELECT audit_finding_id FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:holder AND finding_type_code=:finding_type
              AND rule_key=:rule_key AND finding_status IN ('OPEN','ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "holder": holder_journey_id, "finding_type": _FINDING_TYPE,
         "rule_key": f"{_RULE_PREFIX}:{duplicate_journey_id}"},
    ).scalars().all()
    for finding_id in rows:
        _resolve_finding(
            connection, tenant_id=tenant_id, journey_id=holder_journey_id, stage_code=_STAGE,
            finding_id=finding_id, actor_id=None, correlation_id=correlation_id,
            note="This booking holds the booking; the other one is flagged as the duplicate.",
        )
    return len(rows)


def sync_duplicate_booking_detection(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """Raise DUPLICATE_BOOKING on this journey for every other journey in
    the tenant that looks like the same customer, naming whichever one is
    believed original; resolve it once a pairing no longer matches.
    Idempotent, self-heals, never raises."""
    try:
        signals = _identity_signals(connection, tenant_id=tenant_id, journey_id=journey_id)
        # A journey with nothing identifying left still has to have its own
        # open findings resolved: 2026-10-23, when placeholder values stopped
        # counting as identifiers, the journeys whose only "identifier" was a
        # buyer GSTIN of "No" fell into this branch, and an early return here
        # would have left every wrong pairing open for ever with no way to
        # clear it but by hand. Fall through with no candidates instead.
        candidates = (
            _candidate_journeys(connection, tenant_id=tenant_id, journey_id=journey_id)
            if signals is not None else []
        )
        this_journey_created = connection.execute(
            text("SELECT created_at_utc FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()

        raised = 0
        current_rule_keys: set[str] = set()
        for candidate in candidates:
            basis = _match_basis(signals, candidate)
            if basis is None:
                continue

            this_confirm_date = _booking_confirm_date(
                connection, tenant_id=tenant_id, journey_id=journey_id
            )
            other_confirm_date = _booking_confirm_date(
                connection, tenant_id=tenant_id, journey_id=candidate["journey_id"]
            )
            if this_confirm_date and other_confirm_date:
                if this_confirm_date != other_confirm_date:
                    is_original = this_confirm_date < other_confirm_date
                    originality_basis = "EARLIER_PAYMENT_DATE"
                else:
                    # Same receipt date on both sides -- fall through to the
                    # created_at/journey_id tiebreak below, same as neither
                    # having paid at all.
                    is_original = (this_journey_created, str(journey_id)) <= (
                        candidate["created_at_utc"], str(candidate["journey_id"])
                    )
                    originality_basis = "CREATED_AT_TIEBREAK"
            elif this_confirm_date and not other_confirm_date:
                is_original = True
                originality_basis = "ONLY_THIS_SIDE_PAID"
            elif other_confirm_date and not this_confirm_date:
                is_original = False
                originality_basis = "ONLY_OTHER_SIDE_PAID"
            else:
                # Neither side has reached its minimum booking amount yet --
                # created_at_utc alone can tie (two journeys created in the
                # same transaction share Postgres's transaction-start now()),
                # so journey_id as a stable secondary key guarantees exactly
                # one side of the pair is picked, consistently regardless of
                # which journey's own sync triggered this check.
                is_original = (this_journey_created, str(journey_id)) <= (
                    candidate["created_at_utc"], str(candidate["journey_id"])
                )
                originality_basis = "CREATED_AT_TIEBREAK"

            if is_original:
                continue  # the finding belongs on the duplicate, not this journey

            rule_key = f"{_RULE_PREFIX}:{candidate['journey_id']}"
            current_rule_keys.add(rule_key)
            # A pair carries one finding, on the side that does not hold the
            # booking. The other side may still carry the reverse one from
            # before the booking dates changed (2026-10-20: UTK-SU-261020-004
            # and -001 each named the other as the original).
            _resolve_reverse_finding(
                connection, tenant_id=tenant_id, holder_journey_id=candidate["journey_id"],
                duplicate_journey_id=journey_id, correlation_id=correlation_id,
            )
            _void_open_finding_with_stale_basis(
                connection, tenant_id=tenant_id, journey_id=journey_id,
                rule_key=rule_key, basis=basis, correlation_id=correlation_id,
            )
            _machine_flag(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code=_STAGE,
                rule_key=rule_key,
                finding_type=_FINDING_TYPE,
                severity=_SEVERITY_BY_BASIS[basis],
                title="Possible duplicate booking for the same customer",
                description=(
                    f"This customer appears to match an earlier booking on this "
                    f"tenant ({_BASIS_LABEL[basis]} match). Confirm whether this "
                    "is a genuine repeat customer or a duplicate entry."
                ),
                correlation_id=correlation_id,
                safe_payload={
                    "matchBasis": basis,
                    "believedOriginalJourneyId": str(candidate["journey_id"]),
                    "originalityBasis": originality_basis,
                    "thisBookingConfirmDate": this_confirm_date,
                    "holderBookingConfirmDate": other_confirm_date,
                    "matchConfidencePercent": _CONFIDENCE_PERCENT_BY_BASIS[basis],
                    "matchConfidenceLabel": _CONFIDENCE_LABEL_BY_SEVERITY[_SEVERITY_BY_BASIS[basis]],
                    # The evidence, so the pair can be judged without opening
                    # both journeys (2026-10-23).
                    "matchedFields": _matched_fields(basis, signals, candidate),
                },
            )
            raised += 1

        resolved = 0
        open_findings = connection.execute(
            text(
                """
                SELECT audit_finding_id, rule_key FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND finding_type_code=:finding_type AND finding_status IN ('OPEN','ACKNOWLEDGED')
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "finding_type": _FINDING_TYPE},
        ).mappings().all()
        for finding in open_findings:
            if finding["rule_key"] in current_rule_keys:
                continue
            _resolve_finding(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code=_STAGE,
                finding_id=finding["audit_finding_id"],
                actor_id=None,
                correlation_id=correlation_id,
                note="No longer matches an earlier booking after correction.",
            )
            resolved += 1

        return {"raised": raised, "resolved": resolved, "examined": len(candidates)}
    except Exception:
        logger.warning("sync_duplicate_booking_detection failed", exc_info=True)
        return {"error": True}


__all__ = ["sync_duplicate_booking_detection"]
