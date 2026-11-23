"""An enquiry number is not a booking number (product owner 2026-11-23).

"Enquiry number cant be booking number." Mahindra booking forms carry both, and the reading
put the enquiry number in the booking-number slot on many journeys: "EN 0038100001",
"ENQ - 0038100002", or the bare "0038100003". The DMS keeps them apart -- BOOKING NUMBER
"B-13100009", ENQUIRY NUMBER "EN-0038100003" -- and never prints a booking number of the
enquiry's shape. So an enquiry number taken for a booking number paired two customers' files,
or missed the DMS row of the booking.

A value is an enquiry number when it is
  - written as one: EN / ENQ / ENQUIRY before its digits ("EN-0038100013", "ENQ*27100006"), or
  - of the enquiry's own shape: ten digits starting 00 (the DMS's "EN-00xxxxxxxx"; read with a
    stray prefix too, "BN-0039100008", "SN-0038100010"), or eight digits starting 38 or 39
    (the same number with its zeros dropped, "38100011") that is not written as a B-number.

Such a value is filed as "enquiry_number", never "booking_reference_number"
(uc03_di_core_persistence, uc03_confidence_review_policy); the DMS matcher matches it with the
dump's ENQUIRY NUMBER, after the booking number (uc03_p2_dms_dumps).
"""
from __future__ import annotations

import re
from typing import Any

ENQUIRY_FIELD_KEY = "enquiry_number"
BOOKING_FIELD_KEY = "booking_reference_number"

_WRITTEN_AS_ENQUIRY = re.compile(r"^\s*(ENQUIRY|ENQ|EN)(?![A-Z])", re.IGNORECASE)
_WRITTEN_AS_B_NUMBER = re.compile(r"^\s*B(?![A-Z])", re.IGNORECASE)


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, dict):
        value = value.get("value")
    return str(value).strip() if value is not None else ""


def is_enquiry_number(value: Any) -> bool:
    raw = _text(value)
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return False
    if _WRITTEN_AS_ENQUIRY.match(raw):
        return True
    if len(digits) == 10 and digits.startswith("00"):
        return True
    core = digits.lstrip("0")
    return len(core) == 8 and core[:2] in ("38", "39") and not _WRITTEN_AS_B_NUMBER.match(raw)


def enquiry_key(value: Any) -> str | None:
    """An enquiry number's digits without leading zeros: the key both sides match on."""
    if not is_enquiry_number(value):
        return None
    core = re.sub(r"\D", "", _text(value)).lstrip("0")
    return core if len(core) >= 6 else None


def filed_as(field_key: str, value: Any) -> str:
    """The field a read value is kept under: an enquiry number read where the booking number
    should be is kept as the enquiry number."""
    if field_key == BOOKING_FIELD_KEY and is_enquiry_number(value):
        return ENQUIRY_FIELD_KEY
    return field_key


# The same test in SQL, for the migration that re-files what was kept before (0257) -- keep
# both in step. :v is the value as text.
ENQUIRY_SQL = (
    "((v ~* '^\\s*(ENQUIRY|ENQ|EN)([^A-Za-z]|$)' AND v ~ '[0-9]')"
    " OR regexp_replace(v, '[^0-9]', '', 'g') ~ '^00[0-9]{8}$'"
    " OR (ltrim(regexp_replace(v, '[^0-9]', '', 'g'), '0') ~ '^3[89][0-9]{6}$'"
    "     AND v !~* '^\\s*B([^A-Za-z]|$)'))"
)

__all__ = ["BOOKING_FIELD_KEY", "ENQUIRY_FIELD_KEY", "ENQUIRY_SQL", "enquiry_key", "filed_as", "is_enquiry_number"]
