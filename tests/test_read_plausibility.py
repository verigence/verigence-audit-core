"""Read plausibility (closing list item 4, 2026-11-16)."""
from __future__ import annotations

from datetime import date

from audit_core.uc03_read_plausibility import (
    date_problem,
    name_is_id,
    usable_date,
    usable_name,
    vin_relation,
)

VIN = "MA1RE2TTKT5J31078"
TODAY = date(2026, 11, 16)


def test_dates():
    assert usable_date("2026-10-14", "invoice_date", today=TODAY) == date(2026, 10, 14)
    assert date_problem("14/10/2024", "invoice_date", today=TODAY) == "DATE_BEFORE_FLOOR"  # misread year
    assert date_problem("2026-12-20", "delivery_date", today=TODAY) == "DATE_IN_FUTURE"
    assert date_problem("abc", "receipt_date", today=TODAY) == "DATE_UNREADABLE"
    # A cover that starts ahead is allowed.
    assert date_problem("2026-12-20", "policy_end_date", today=TODAY) is None
    assert usable_date(None) is None and date_problem("") is None


def test_vin():
    assert vin_relation(VIN, VIN) == "SAME"
    assert vin_relation("T5J31078", VIN) == "SAME"          # the printed ending
    assert vin_relation("MA1RE2TTKT5J31O78", VIN) == "MISREAD"  # O for 0
    assert vin_relation("T5J3I07B", VIN) == "MISREAD"       # two characters off the ending
    assert vin_relation("MA1XU2TTKS7K90011", VIN) == "OTHER"
    assert vin_relation("T5J", VIN) == "UNKNOWN"
    assert vin_relation(VIN, None) == "UNKNOWN"


def test_names():
    assert name_is_id("6f1c2b9e-1d2a-4c3b-9e8f-0a1b2c3d4e5f")
    assert name_is_id("1015544")
    assert not name_is_id("JASMITA PANI")
    assert usable_name("  JASMITA   PANI ") == "JASMITA PANI"
    assert usable_name("6f1c2b9e1d2a4c3b9e8f0a1b2c3d4e5f") is None
