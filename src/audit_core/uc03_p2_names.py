"""Names as documents print them, compared the way a person would.

A booking form, an invoice, a receipt and an insurance cover note all name
the customer, and the KYC (PAN, Aadhaar) says who the customer is. The
same person appears as "Mr. Biswabhanu Biswal", "BISWAL BISWABHANU" or
"B. Biswal": honorifics, order and initials are not a different customer.
A different person is. Dealer names differ in the same ways ("Sarthak
Motors Pvt. Ltd." against "SARTHAK MOTORS").
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

_HONORIFICS = frozenset({
    "MR", "MRS", "MS", "MISS", "MX", "DR", "SHRI", "SRI", "SMT", "KUMARI", "KU", "MASTER", "PROF",
    "M/S", "MS.", "MESSRS",
})
_ORG_SUFFIXES = frozenset({
    "PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "INC", "CO", "COMPANY", "CORP", "CORPORATION",
    "AND", "&", "THE", "PVTLTD",
})
# "Biswabhanu Biswal S/O Kailash Biswal": the relation after S/O, D/O, W/O,
# C/O is not part of the name.
_RELATION = re.compile(r"\b(S|D|W|C)\s*/\s*O\b.*$", re.IGNORECASE)
_NOISE = re.compile(r"[^A-Z0-9&/ ]+")
# The joined strings may differ by one misread letter ("BISWAL" / "BISWAI"):
# that is still the same customer, a different surname is not.
_SAME_RATIO = 0.92


def _tokens(value: Any, *, drop: frozenset[str]) -> list[str]:
    text = _RELATION.sub("", str(value or "").upper())
    text = text.replace("M/S", " ").replace("M/S.", " ")
    text = _NOISE.sub(" ", text)
    return [t for t in text.split() if t and t not in drop]


def person_tokens(value: Any) -> list[str]:
    return _tokens(value, drop=_HONORIFICS)


def org_tokens(value: Any) -> list[str]:
    return _tokens(value, drop=_ORG_SUFFIXES)


def _initial_matches(short: str, full: str) -> bool:
    return len(short) == 1 and full.startswith(short)


def _tokens_match(left: list[str], right: list[str]) -> bool:
    """Every token on the shorter side has a counterpart on the longer side
    (the same word, or an initial of it), and at least one whole word is
    shared, so "A. Sahoo" is Anita Sahoo but "A. B." is nobody."""
    if not left or not right:
        return False
    if sorted(left) == sorted(right):
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    unused = list(longer)
    whole = 0
    for token in shorter:
        hit = next((t for t in unused if t == token), None)
        if hit is None:
            hit = next((t for t in unused if _initial_matches(token, t) or _initial_matches(t, token)), None)
        else:
            whole += 1
        if hit is None:
            return False
        unused.remove(hit)
    return whole > 0


def _close_enough(left: list[str], right: list[str]) -> bool:
    a, b = " ".join(sorted(left)), " ".join(sorted(right))
    return bool(a and b) and SequenceMatcher(None, a, b).ratio() >= _SAME_RATIO


def same_person(left: Any, right: Any) -> bool:
    """Whether two printed names are the same customer."""
    a, b = person_tokens(left), person_tokens(right)
    return _tokens_match(a, b) or _close_enough(a, b)


def same_organisation(left: Any, right: Any) -> bool:
    """Whether two printed names are the same dealership: the same words
    once the legal suffixes go, one a prefix of the other ("Sarthak Motors"
    and "Sarthak Motors Bhubaneswar"), or one letter apart."""
    a, b = org_tokens(left), org_tokens(right)
    if not a or not b:
        return False
    if a == b or a[: len(b)] == b or b[: len(a)] == a:
        return True
    return _tokens_match(a, b) or _close_enough(a, b)


def display_name(value: Any) -> str:
    """The name as printed, trimmed, for a task the PC reads."""
    return " ".join(str(value or "").split()) or "blank"
