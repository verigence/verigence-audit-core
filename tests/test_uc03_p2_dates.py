"""Dates as the extractor writes them, and the date floor that catches a
misread one (a PAN or receipt dated 2019 on a 2026 booking)."""

from __future__ import annotations

from datetime import date

import pytest

from audit_core.uc03_p2_dates import date_floor_verdict, parse_extracted_date
from audit_core.uc03_p2_registry import get_registry

FLOOR = date(2026, 7, 1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-01", date(2026, 9, 1)),
        ("2026-09-01T10:00:00Z", date(2026, 9, 1)),
        ("2026/09/01", date(2026, 9, 1)),
        ("01/09/2026", date(2026, 9, 1)),  # day first, as Indian documents write it
        ("01-09-2026", date(2026, 9, 1)),
        ("01.09.2026", date(2026, 9, 1)),
        ("13/09/26", date(2026, 9, 13)),
        ("09/13/2026", date(2026, 9, 13)),  # only a day can be 13
        ("1 Sep 2026", date(2026, 9, 1)),
        ("01-Sep-2026", date(2026, 9, 1)),
        ("1st September, 2026", date(2026, 9, 1)),
        ("September 1, 2026", date(2026, 9, 1)),
        ("Mon, 01 Sep 2026 10:30", date(2026, 9, 1)),
        (date(2026, 9, 1), date(2026, 9, 1)),
        ("31/02/2026", None),
        ("not a date", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_extracted_date(value, expected):
    assert parse_extracted_date(value) == expected


def test_date_floor_verdict():
    assert date_floor_verdict("01/09/2026", FLOOR) is None
    assert date_floor_verdict("2026-07-01", FLOOR) is None
    assert date_floor_verdict("30/06/2026", FLOOR) == "DATE_BEFORE_FLOOR"
    assert date_floor_verdict("12/03/2019", FLOOR) == "DATE_BEFORE_FLOOR"
    assert date_floor_verdict("garbage", FLOOR) == "DATE_UNREADABLE"
    assert date_floor_verdict("", FLOOR) is None
    assert date_floor_verdict(None, FLOOR) is None
    assert date_floor_verdict("12/03/2019", None) is None  # no floor configured


def test_registry_date_floor_applies_to_document_dates_not_birth_dates():
    registry = get_registry()
    assert registry.extraction_rules.date_floor == FLOOR
    assert registry.date_check_applies("dealer_receipt", "receipt_date")
    assert registry.date_check_applies("booking_form", "booking_date")
    assert registry.date_check_applies("gate_pass", "delivery_date")  # typed string, named a date
    assert registry.date_check_applies("insurance_cover", "policy_start_date")
    assert registry.date_check_applies("upi_screenshot", "transaction_datetime")
    assert not registry.date_check_applies("aadhaar", "date_of_birth")
    assert not registry.date_check_applies("pan_card", "date_of_birth")
    assert not registry.date_check_applies("corporate_id", "date_of_joining")
    assert not registry.date_check_applies("valuation_report", "registration_date")
    assert not registry.date_check_applies("gst_certificate", "date_of_issue")
    assert not registry.date_check_applies("pan_card", "pan_number")
    assert not registry.date_check_applies("dealer_receipt", "amount_paid")
