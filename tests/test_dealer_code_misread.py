"""A dealer's MISP code read with a character or two wrong is still the dealer's (2026-11-16).

Utkal's MIBL/M&M/AAACU2665C/000 was read as AAECU2665C / AAECA2665C / AAACU2656C /
AAUC2665C on 40 policies: the insurance showed as unconfirmed, or Self where the
docket said so, though the policy carries the dealer's own MISP code."""
from __future__ import annotations

import pytest

from audit_core.uc03_insurance_source import dealer_code_match, normalize_code

UTKAL = {normalize_code("MIBL/M&M/AAACU2665C/000"), "1471280493"}
ADITYA = {normalize_code("MIBL/M&M/AAECA5786A/000")}


@pytest.mark.parametrize("read", ["MIBL/M&M/AAECU2665C/000", "MIBL/M&M/AAECA2665C/000",
                                  "MIBL/M&M/AAACU2656C/000", "MIBL/M&M/AAUC2665C/000",
                                  "MIBL/MSM/AAACU2665C/000", "MIBL/M&M/AAACU2665C-000"])
def test_a_misread_of_the_dealers_misp_code_is_the_dealers(read):
    match = dealer_code_match(read, UTKAL, ADITYA)
    assert match is not None and match[0] == normalize_code("MIBL/M&M/AAACU2665C/000") and match[1] <= 2


def test_the_exact_code_is_a_zero_misread():
    assert dealer_code_match("MIBL/M&M/AAACU2665C/000", UTKAL, ADITYA)[1] == 0
    assert dealer_code_match("1471280493", UTKAL, ADITYA) == ("1471280493", 0)


def test_another_dealers_code_or_a_short_code_is_never_taken():
    assert dealer_code_match("MIBL/M&M/AAECA5786A/000", UTKAL, ADITYA) is None
    assert dealer_code_match("1471280494", UTKAL, ADITYA) is None          # short codes: exact only
    assert dealer_code_match("MIBL/M&M/AAXYZ9999C/000", UTKAL, ADITYA) is None


def test_a_code_as_near_another_dealers_is_left_to_the_pc():
    own = {normalize_code("MIBL/M&M/AAACU2665C/000")}
    other = {normalize_code("MIBL/M&M/AAACU2665D/000")}
    assert dealer_code_match("MIBL/M&M/AAACU2665X/000", own, other) is None
