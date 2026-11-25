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


def test_a_duplicate_journey_is_the_same_otf_or_the_same_customer_and_sale():
    """2026-11-22: two journeys of one sale (or one holding the other's OTF) leave the DMS row to
    neither; a fleet buyer's second model, or a namesake months apart, is not a duplicate."""
    from audit_core.uc03_p2_dms_booking_crosscheck import duplicate_of

    day = {date(2026, 11, 6)}
    me = _journey(journeyId="a", reference="J-A", refs={"77700012"}, model="SCORPIO N", bookingDays=day)
    others = {"a": me,
              "b": _journey(journeyId="b", reference="J-B", names={"Other Person"}, refs={"77700012"},
                            model="SCORPIO N")}
    assert duplicate_of(me, others)["reference"] == "J-B"  # the same OTF
    me = _journey(journeyId="a", reference="J-A", model="MAXX HD", bookingDays=day)
    twin = _journey(journeyId="c", reference="J-C", model="MAXX HD", bookingDays={date(2026, 11, 7)})
    assert duplicate_of(me, {"a": me, "c": twin})["reference"] == "J-C"  # same customer and model
    fleet = _journey(journeyId="d", reference="J-D", model="BOLERO NEO", bookingDays=day)
    assert duplicate_of(me, {"a": me, "d": fleet}) is None  # a second model: another sale
    namesake = _journey(journeyId="e", reference="J-E", model="MAXX HD", bookingDays={date(2026, 6, 1)})
    assert duplicate_of(me, {"a": me, "e": namesake}) is None  # months apart
    unread = _journey(journeyId="f", reference="J-F", model=None, mobiles={"9000000001"}, bookingDays=day)
    me = _journey(journeyId="a", reference="J-A", model="MAXX HD", mobiles={"9000000001"}, bookingDays=day)
    assert duplicate_of(me, {"a": me, "f": unread})["reference"] == "J-F"  # model unread: the mobile decides


def test_alike_journeys_take_alike_rows_one_each():
    """2026-11-22: one customer's two identical MAXX HD (two DMS rows, two Journeys) were left
    unmatched on both sides; one booking uploaded twice left its DMS row unmatched."""
    from audit_core.uc03_p2_dms_dumps import BOOKING, JourneyIndex, match_row

    index = JourneyIndex()
    for jid, ref in (("j1", "J-1"), ("j2", "J-2")):
        index.journeys[jid] = {"journeyId": jid, "reference": ref, "names": {"CUSTOMER K"}, "model": "MAXX HD",
                               "mobiles": {"9000012345"}, "refs": set(), "bookingDays": set()}
        index.by_mobile["9000012345"].add(jid)
    row = {"customer_name": "CUSTOMER K", "mobile": "9000012345", "model": "MAXX HD",
           "event_date": date(2026, 11, 6)}
    assert match_row(index, BOOKING, row) == ("j1", "MOBILE_NAME")
    assert match_row(index, BOOKING, row, {"j1"}) == ("j2", "MOBILE_NAME")
    # Another customer on the same mobile: not alike, no guess.
    index.journeys["j2"]["names"] = {"Someone Else"}
    assert match_row(index, BOOKING, {**row, "customer_name": "Third Name"}) == (None, None)



def test_the_dms_enquiry_number_matches_after_the_booking_number():
    """2026-11-23: the booking form's enquiry number, never a booking number, matches the dump's
    ENQUIRY NUMBER column -- and a booking number still wins over it."""
    from audit_core.uc03_p2_dms_dumps import BOOKING, JourneyIndex, match_row

    index = JourneyIndex()
    index.journeys["j1"] = {"journeyId": "j1", "reference": "J-1", "names": {"CUSTOMER E"}, "model": "MAXX HD",
                            "mobiles": set(), "refs": set(), "bookingDays": set()}
    index.by_enquiry["38100003"].add("j1")
    row = {"customer_name": "CUSTOMER E", "booking_number": "B-13100009", "model": "MAXX HD",
           "raw": {"ENQUIRY NUMBER": "EN-0038100003"}}
    assert match_row(index, BOOKING, row) == ("j1", "ENQUIRY_NUMBER")
    index.journeys["j2"] = {"journeyId": "j2", "reference": "J-2", "names": {"CUSTOMER F"}, "model": "MAXX HD",
                            "mobiles": set(), "refs": {"13100009"}, "bookingDays": set()}
    index.by_ref["13100009"].add("j2")
    assert match_row(index, BOOKING, row) == ("j2", "BOOKING_NUMBER")
