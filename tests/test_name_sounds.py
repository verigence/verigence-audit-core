"""Names compared as Odisha documents spell them (2026-11-16, live "customer name differs" tasks)."""
from __future__ import annotations

import pytest

from audit_core.uc03_p2_names import same_person, sound_key


@pytest.mark.parametrize(("left", "right"), [
    ("Sanjip ku sahoo", "SANJIB KUMAR SAHOO"),
    ("Girish ch. Biswal..", "GIRISH CHANDRA BISWAL"),
    ("MD MUMTAZ", "MOHAMMAD MUMTAZ"),
    ("Mohit Sahoo", "MOHIT SAHU"),
    ("Dillip Kumar Nayak", "DILIP NAYAK"),
    ("SOUMYARANJAN MOHANTY", "SAUMYA RANJAN MOHANTY"),
    ("N.Santosh Kumar Choudhury", "NARAYANSINTI SANTOSH KUMAR CHHOUDHURY"),
    ("TUSHARKANT MOHANTY", "TUSAR KANTA MOHANTY"),
    ("MR. RAMA KRISHNA PATTNAYAK", "RAMAKRUSHNA PATTNAYAK"),
    ("PRABHATA SAHU", "Pravata Sahu"),
    ("Ku. Priya Das", "PRIYA DAS"),
])
def test_the_same_person(left, right):
    assert same_person(left, right)


@pytest.mark.parametrize(("left", "right"), [
    ("Hamida Begum", "AFROJ ANSARI"),
    ("Gita Dash", "SITA DASH"),
    ("SUNIL BEHERA", "SUJATA BEHERA"),
    ("Debashis Bastia", "Debadatta Bastia"),
    ("Dhiraj Kumar Singh", "DHIRAJ KUMAR GUPTA"),
    ("Prabhat Sahoo", "PRAKASH SAHOO"),
    ("Suresh Chandra Patra", "Suresh Chandra Dash"),
    ("Sandipta Kumar Das", "SANDIPTA KUMAR BAL"),
    ("MANOJ NAYAK", "Pankaj Kumar Nayak"),
    ("Bhabani Sankar Sasmal", "BANITA SASMAL"),
    # Live 2026-11-16: one letter apart on a long name, but the first letter of a word.
    ("BHARAT KUMAR SUBUDHI", "SARAT KUMAR SUBUDHI"),
    ("BHARAT KUMAR SUBUDHI", "Mr.Sarat Ku Subudhi"),
    ("Md Golam Ahamad Reza", "MD GOLAM SAMAD REZA"),
])
def test_different_people(left, right):
    assert not same_person(left, right)


def test_sound_key():
    assert sound_key("SAHOO") == sound_key("SAHU")
    assert sound_key("CHHOUDHURY") == sound_key("CHOUDHURY")
    assert sound_key("GITA") != sound_key("SITA")
