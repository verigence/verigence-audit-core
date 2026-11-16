"""Read plausibility (closing list item 4, product owner 2026-11-16).

One place decides whether a value read from a document can be used by a
check. A value that cannot be right as read counts as NOT READ: no check
uses it -- the check waits instead of failing on it -- and the PC is asked
to check the reading on the document's read-check task (the field review,
uc03_p2_stage.field_review_reasons).

What cannot be right as read:
- a date that is not a date, lies before the programme's floor (a misread
  year: 2021, 2023, 2024 on a 2026 deal), or lies after today for an event
  that has already happened (a booking, an invoice, a receipt, a gate pass);
- a VIN or chassis number that differs from the Journey's agreed VIN
  (uc03_p2_journey_classification.journey_vin) by one to three characters:
  a misread of the same vehicle, never another vehicle;
- a customer name that is an id (a UUID or a bare number), never a name.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from audit_core.uc03_p2_dates import (
    cannot_be_future,
    date_floor_verdict,
    parse_extracted_date,
    programme_today,
)

# Up to this many characters different from the agreed VIN is a misread of it.
MAX_VIN_MISREAD = 3
# The shortest chassis ending worth comparing (accessory invoices print the last 8).
MIN_CHASSIS_SUFFIX = 6
VIN_MISREAD = "VIN_MISREAD"
_UUID = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.IGNORECASE)


def _floor() -> date | None:
    from audit_core.uc03_p2_registry import get_registry

    return get_registry().extraction_rules.date_floor


def date_problem(value: Any, field_key: str = "", *, today: date | None = None) -> str | None:
    """Why a date as read cannot be used: DATE_UNREADABLE, DATE_BEFORE_FLOOR or
    DATE_IN_FUTURE; None when it can (or there is no value)."""
    if value is None or str(value).strip() in ("", "null"):
        return None
    verdict = date_floor_verdict(value, _floor())
    if verdict:
        return verdict
    day = parse_extracted_date(value)
    if day is None:
        return "DATE_UNREADABLE"
    if (cannot_be_future(field_key) or not field_key) and day > (today or programme_today()):
        return "DATE_IN_FUTURE"
    return None


def usable_date(value: Any, field_key: str = "", *, today: date | None = None) -> date | None:
    """The date as read, or None when it is not read or cannot be right."""
    if date_problem(value, field_key, today=today):
        return None
    return parse_extracted_date(value)


def clean_chassis(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def vin_relation(value: Any, agreed: str | None) -> str:
    """How a VIN / chassis reading relates to the Journey's agreed VIN:
    SAME (the VIN or its own ending), MISREAD (1-3 characters off),
    OTHER (another vehicle), or UNKNOWN (nothing to compare)."""
    printed, vin = clean_chassis(value), clean_chassis(agreed)
    if len(printed) < MIN_CHASSIS_SUFFIX or not vin:
        return "UNKNOWN"
    short, full = sorted((printed, vin), key=len)
    if full.endswith(short):
        return "SAME"
    if len(short) == len(full) or len(short) < len(full):
        tail = full[-len(short):]
        differences = sum(a != b for a, b in zip(short, tail, strict=True))
        if differences <= MAX_VIN_MISREAD:
            return "MISREAD"
    return "OTHER"


def name_is_id(value: Any) -> bool:
    """A customer "name" that is an id: a UUID, or digits only."""
    text = str(value or "").strip()
    return bool(text) and (bool(_UUID.match(text)) or text.replace(" ", "").isdigit())


def usable_name(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return None if not text or name_is_id(text) else text


__all__ = ["MAX_VIN_MISREAD", "VIN_MISREAD", "clean_chassis", "date_problem", "name_is_id", "usable_date",
           "usable_name", "vin_relation"]
