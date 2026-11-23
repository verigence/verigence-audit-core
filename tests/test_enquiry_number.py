"""An enquiry number is not a booking number (product owner 2026-11-23)."""
from __future__ import annotations

import pytest

from audit_core.uc03_enquiry_number import enquiry_key, filed_as, is_enquiry_number


@pytest.mark.parametrize("value", [
    "EN 0038100001", "EN - 0038100004", "EN- 00 38100005", "ENQ - 0038100002", "ENQ*27100006",
    "ENQ 830010000007", "ENQUIRY NO 0038000001", "0038100003", "BN-0039100008", "SN-0038100010",
    "38100011", "R0039100012",
])
def test_an_enquiry_number(value):
    assert is_enquiry_number(value)
    assert filed_as("booking_reference_number", value) == "enquiry_number"


@pytest.mark.parametrize("value", [
    "B-13100014", "13100015", "B13100015", "OTF27A100016", "07F10017", "AMC-B/10018/26-27", "144",
    "B-38100020",  # written as a B-number
    "ENGINE 12345", None, "", "N/A",
])
def test_not_an_enquiry_number(value):
    assert not is_enquiry_number(value)
    assert filed_as("booking_reference_number", value) == "booking_reference_number"


def test_only_the_booking_number_field_is_refiled():
    assert filed_as("receipt_number", "0038100003") == "receipt_number"


def test_both_sides_match_on_the_same_key():
    assert enquiry_key("EN-0038100003") == enquiry_key("0038100003") == enquiry_key("38100003") == "38100003"
    assert enquiry_key("B-13100014") is None
