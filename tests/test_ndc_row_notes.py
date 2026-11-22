"""A note written after an NDC row's figure is not a working (2026-11-22).

Pure: no database. Rows as read off live Mahindra Orissa account statements."""
from __future__ import annotations

from decimal import Decimal

import pytest

from audit_core.uc03_account_statement import read_statement


def _read(label: str, amount: int):
    return read_statement([{"particulars_raw": label, "debit_amount": amount}])


@pytest.mark.parametrize(("label", "amount", "key"), [
    # ADI-RD-261112-021: the 7.2 kW wall-box charger, Rs 60,000 on the accessory invoice too
    ("ACCESSORIES 7.2 Charger", 60000, "accessories_cost"),
    ("ACCESSORIES 3 PARTY", 5000, "accessories_cost"),
    ("RSA (2YR)", 2021, "rsa_amount"),
    ("RSA 4 YRS", 940, "rsa_amount"),
])
def test_words_and_small_numbers_after_the_item_are_a_note(label, amount, key):
    read = _read(label, amount)
    assert read.components == {key: Decimal(amount)}
    assert not read.unmapped


@pytest.mark.parametrize(("label", "amount"), [
    ("ACCESSORIES 5000/ FOC", 4918),
    ("ACCESSORIES SCHEME 5000 FREE ACCESSORIES", 1520),
    ("ACCESSORIES 1 DEC 5000", 1025),
])
def test_an_amount_or_a_given_word_keeps_it_a_working_left_for_a_person(label, amount):
    read = _read(label, amount)
    assert "accessories_cost" not in read.components
    assert read.unmapped and read.unmapped[0]["why"] == "the working written beside it does not agree with the amount"


def test_a_working_that_adds_up_is_still_read():
    read = _read("ACCESSORIES 48398-18398 FREE", 30000)
    assert read.components["accessories_cost"] == Decimal(48398)
