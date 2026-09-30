"""Dates as the extractor writes them.

The extractor returns dates as text in whatever shape the page used
(``2026-09-01``, ``01/09/2026``, ``1 Sep 2026``, ``01-Sep-26``...). The
audit needs one answer from any of them: which day is it, or that the
value is not a date at all. Indian documents write day before month, so
``03/04/2026`` is 3 April; a first part above 12 or an unambiguous month
name settles the order either way.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[t ].*)?$")
_YMD = re.compile(r"^(\d{4})[/.](\d{1,2})[/.](\d{1,2})$")
_DMY = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})$")
_DMY_NAME = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s/.\-]*([a-z]+)[\s/.\-,]*(\d{2}|\d{4})$")
_MDY_NAME = re.compile(r"^([a-z]+)[\s/.\-]*(\d{1,2})(?:st|nd|rd|th)?[\s/.\-,]*(\d{2}|\d{4})$")


def _year(text: str) -> int:
    year = int(text)
    return 2000 + year if year < 100 else year


def _build(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_extracted_date(value: Any) -> date | None:
    """The day an extracted value names, or None when it is not a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().lower()
    # A leading time or weekday adds nothing: "mon, 01 sep 2026 10:30" -> "01 sep 2026 10:30".
    text = re.sub(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+", "", text)
    text = re.sub(r"\s+(at\s+)?\d{1,2}:\d{2}(:\d{2})?\s*(am|pm|ist|utc)?$", "", text)
    if not text:
        return None
    if match := _ISO.match(text):
        return _build(int(match[1]), int(match[2]), int(match[3]))
    if match := _YMD.match(text):
        return _build(int(match[1]), int(match[2]), int(match[3]))
    if match := _DMY.match(text):
        first, second, year = int(match[1]), int(match[2]), _year(match[3])
        # Day first, as Indian documents write it; a first part past 12 can
        # only be a day, a second part past 12 can only be a day.
        if first > 12 and second <= 12:
            return _build(year, second, first)
        if second > 12 and first <= 12:
            return _build(year, first, second)
        return _build(year, second, first)
    if match := _DMY_NAME.match(text):
        month = _MONTHS.get(match[2])
        return _build(_year(match[3]), month, int(match[1])) if month else None
    if match := _MDY_NAME.match(text):
        month = _MONTHS.get(match[1])
        return _build(_year(match[3]), month, int(match[2])) if month else None
    return None


def date_floor_verdict(value: Any, floor: date | None) -> str | None:
    """Why a date value cannot be trusted as read, or None when it can.

    ``DATE_BEFORE_FLOOR``: the day is earlier than the floor, which a
    document of this Journey cannot carry, so a digit or the year was
    misread. ``DATE_UNREADABLE``: the value is not a date at all.
    """
    if floor is None or value is None or str(value).strip() == "":
        return None
    parsed = parse_extracted_date(value)
    if parsed is None:
        return "DATE_UNREADABLE"
    if parsed < floor:
        return "DATE_BEFORE_FLOOR"
    return None


def booking_form_date(connection: Any, *, tenant_id: str, journey_id: Any) -> dict[str, Any] | None:
    """The booking date as the booking form (or docket) carries it, the
    PC's correction winning over the reading: {"documentId", "documentType",
    "raw", "date"}, ``date`` None when the form has no readable booking
    date; None when no booking form is on file. The deal is priced on this
    date, so it is never guessed (decision 2026-09-30: no fallback to
    today; a Medium task asks the PC to enter a missing one)."""
    from audit_core.uc03_p2_journey360 import _document_facts

    documents = _document_facts(
        connection, tenant_id=tenant_id, journey_id=journey_id, di_types=("booking_form", "booking_docket"),
    )
    if not documents:
        return None
    form = next((d for d in documents if d["documentType"] == "booking_form"), documents[0])
    raw = form["fields"].get("booking_date")
    return {
        "documentId": form["documentId"],
        "documentType": form["documentType"],
        "raw": raw,
        "date": parse_extracted_date(raw),
    }
