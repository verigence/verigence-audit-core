"""A booking in Verigence no DMS row found, and why (product owner 2026-11-22: "Verigence can't
have additional; if it is additional, we need to cross check where is the issue in match")."""
from __future__ import annotations

from datetime import date

from audit_core.uc03_p2_dms_booking_crosscheck import diagnose


def _journey(**kw) -> dict:
    return {"journeyId": "mine", "refs": set(), "mobiles": set(), "names": {"Customer A"}, "model": "THAR", **kw}


def _row(n: int, **kw) -> dict:
    return {"row_no": n, "outcome": "MISSING", "journey_id": None, "customer_name": "Someone Else", "mobile": None,
            "booking_number": None, "dms_booking_number": None, "model": "XUV 3XO", "event_date": date(2026, 11, 5),
            "matched_to": None, **kw}


def test_the_otf_on_another_journey_is_a_duplicate_or_a_wrong_otf():
    rows = [_row(1, dms_booking_number="OTF27A012345", outcome="MATCHED", journey_id="other", matched_to="J-2")]
    code, why, near = diagnose(_journey(refs={"12345"}), rows)
    assert code == "OTF_ON_ANOTHER_JOURNEY" and near["row_no"] == 1 and "J-2" in why


def test_a_cancelled_otf():
    rows = [_row(1, dms_booking_number="OTF27A012345", outcome="CANCELLED")]
    assert diagnose(_journey(refs={"12345"}), rows)[0] == "OTF_CANCELLED"


def test_an_otf_a_digit_off_with_the_same_name_is_misread():
    rows = [_row(1, dms_booking_number="OTF27A012346", customer_name="CUSTOMER A")]
    assert diagnose(_journey(refs={"012345"}), rows)[0] == "OTF_MISREAD"


def test_the_mobile_then_the_name():
    rows = [_row(1, mobile="9000000001", outcome="MATCHED", journey_id="other", matched_to="J-3")]
    assert diagnose(_journey(mobiles={"9000000001"}), rows)[0] == "MOBILE_ON_ANOTHER_JOURNEY"
    rows = [_row(2, mobile="9000000001")]
    assert diagnose(_journey(mobiles={"9000000001"}), rows)[0] == "MOBILE_MODEL_DIFFERS"
    rows = [_row(3, customer_name="CUSTOMER A", model="SCORPIO N")]
    assert diagnose(_journey(), rows)[0] == "NAME_MODEL_DIFFERS"
    rows = [_row(4, mobile="9000000009")]
    assert diagnose(_journey(mobiles={"9000000001"}), rows)[0] == "MOBILE_MISREAD"


def test_nothing_agrees():
    assert diagnose(_journey(refs={"55555"}), [_row(1)]) == (
        "NOT_IN_DMS", diagnose(_journey(refs={"55555"}), [_row(1)])[1], None)
    assert diagnose(_journey(), [_row(1)])[0] == "OTF_NOT_READ"
