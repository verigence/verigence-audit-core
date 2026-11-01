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
    # A word with a digit in it is not part of a name: receipts print the
    # customer as "RASHMI RANJAN BISWAL- R270908806" or "GANGA NARAYAN
    # NANDA-AGKPN5802B" (the receipt number, the PAN) (2026-10-28).
    return [t for t in text.split() if t and t not in drop and not any(c.isdigit() for c in t)]


def _one_letter_apart(a: str, b: str) -> bool:
    """One letter misread, missed, added, or two neighbours swapped."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                  and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


# Only a word this long may differ by one letter: in a short word one letter
# is a different word.
_MISREAD_MIN = 5


def _misread_once(left: list[str], right: list[str]) -> bool:
    """The same words in the same order but one, which is one letter apart
    ("BINAY KUMAR BEHERA" / "Binay Kumar Behar", "PRITI RANJAN PANDA" /
    "PRITI RNAJAN PANDA"). The module's rule -- one misread letter is still
    the customer, a different surname is not -- applied word by word, since
    on a short name the whole-string ratio cannot tell one letter from two."""
    if len(left) != len(right) or len(left) < 2:
        return False
    different = [(a, b) for a, b in zip(left, right, strict=True) if a != b]
    return (len(different) == 1 and min(len(different[0][0]), len(different[0][1])) >= _MISREAD_MIN - 1
            and max(len(different[0][0]), len(different[0][1])) >= _MISREAD_MIN
            and _one_letter_apart(*different[0]))


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
    """Whether two printed names are the same customer.

    Also (2026-10-28): the same letters split differently ("RASMIRANJAN" /
    "RASMI RANJAN"), one word one letter apart, and -- for a company buying --
    the same company once the legal suffixes go ("PVT LTD" / "PRIVATE LIMITED")."""
    a, b = person_tokens(left), person_tokens(right)
    if _tokens_match(a, b) or _close_enough(a, b) or _misread_once(a, b):
        return True
    if a and b and "".join(a) == "".join(b):
        return True
    if _is_company(left) or _is_company(right):
        return same_organisation(left, right)
    return False


_COMPANY_WORDS = frozenset({"PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "PVTLTD", "TRUST", "SOCIETY",
                            "ENTERPRISES", "CORPORATION", "COMPANY", "INDUSTRIES"})


def _is_company(value: Any) -> bool:
    return bool(set(_NOISE.sub(" ", str(value or "").upper()).split()) & _COMPANY_WORDS)


# A PAN read with its letters masked, as some PAN copies print or DigiLocker
# shows them: "PXXXXJ KXXXR NXXXX" (product owner 2026-10-28).
_MASKED_TOKEN = re.compile(r"[A-Za-z]X{2,}")


def is_masked(value: Any) -> bool:
    """Whether a printed name has its letters masked with X."""
    return bool(_MASKED_TOKEN.search(str(value or "")))


def masked_matches(masked: Any, full: Any) -> bool:
    """Whether a masked name fits a full one: the same words, each of the same
    length, every letter that is not masked the same ("MXXXXI JXXa" is Minati Jena).
    The full name's honorific is not a word of the name (2026-11-01: "Mr.
    Sushanta Sahoo" on a booking form fits "SXXXXXXA SXXXO"; a PAN prints none)."""
    left = [t for t in re.sub(r"[^A-Za-z ]+", " ", str(masked or "")).upper().split() if t]
    right = [t for t in re.sub(r"[^A-Za-z ]+", " ", str(full or "")).upper().split() if t]
    if not left:
        return False

    def fits(words: list[str]) -> bool:
        return len(left) == len(words) and all(
            len(m) == len(f) and all(a == "X" or a == b for a, b in zip(m, f, strict=True))
            for m, f in zip(left, words, strict=True))

    return fits(right) or fits(person_tokens(full))


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


def dealership_word(value: Any) -> str | None:
    """The distinctive first word of a dealership's name ("Shivnath Motors"
    -> "SHIVNATH"), or None when it is too short to tell dealerships apart."""
    tokens = org_tokens(value)
    return tokens[0] if tokens and len(tokens[0]) >= _MISREAD_MIN else None


def same_dealership_group(left: Any, right: Any, master_words: list[str]) -> bool:
    """Whether two printed dealer names are variants of one dealership of the
    tenant's dealer master (product owner 2026-10-30: "Shivnath Group",
    "Shivnath Motors (India) Pvt Ltd" and "Shivanath Motors" are one
    dealership for now). Both names must start with the master's distinctive
    word, one letter of misreading allowed."""
    a, b = org_tokens(left), org_tokens(right)
    if not a or not b:
        return False
    for word in master_words:
        if _one_letter_apart(a[0], word) and _one_letter_apart(b[0], word):
            return True
    return False


# A firm's name, as a proprietor's business prints on its documents
# ("M/S SHREE SHYAM ENTERPRISES", "MAA MANGALA TRADERS", "TARA BAKERY").
_FIRM_PREFIX = re.compile(r"^\s*(M\s*/\s*S\b|MESSRS\b)", re.IGNORECASE)
_FIRM_WORDS = frozenset({
    "ENTERPRISE", "ENTERPRISES", "ENTERPRISERS", "TRADERS", "TRADER", "TRADING", "STORE", "STORES", "SUPPLIER",
    "SUPPLIERS", "AGENCY", "AGENCIES", "BAKERY", "BOREWELL", "CONSTRUCTION", "CONSTRUCTIONS", "TRANSPORT",
    "TRANSPORTS", "SERVICE", "SERVICES", "MARBLE", "MARBLES", "TILES", "GLASS", "ASSOCIATES", "AGRO", "ELECTRICAL",
    "ELECTRICALS", "WORKSHOP", "INDUSTRIES", "INDUSTRY", "UDYOG", "ADVERTISING", "CORPORATION", "CENTRE", "CENTER",
    "MEDICAL", "MEDICALS", "HARDWARE", "BUILDERS", "DEVELOPERS", "PROJECTS", "INFRA", "INFRACON", "LOGISTICS",
    "ROADLINES", "CARRIERS", "SOLUTIONS", "FOODS", "HOTEL", "RESTAURANT", "FURNITURE", "JEWELLERS", "TEXTILES",
    "PHARMA", "CLINIC", "HOSPITAL", "SCHOOL", "TRUST", "SOCIETY", "ENGINEERING", "ENGINEERS", "WORKS", "MART",
    "EMPORIUM", "BHANDAR", "SHOP", "PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "COMPANY", "DEPARTMENT", "FIRM",
})


def is_firm_name(value: Any) -> bool:
    """Whether a printed name is a firm's rather than a person's (2026-10-28)."""
    text = str(value or "")
    if _FIRM_PREFIX.search(text):
        return True
    words = set(_NOISE.sub(" ", text.upper()).split())
    return bool(words & _FIRM_WORDS)


def display_name(value: Any) -> str:
    """The name as printed, trimmed, for a task the PC reads."""
    return " ".join(str(value or "").split()) or "blank"
