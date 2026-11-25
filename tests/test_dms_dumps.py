"""The DMS booking / delivery dumps: reading each dealer's layout and matching rows
to Journeys (product owner 2026-11-13). Pure: no database, synthetic rows in the
layouts of the 5 Nov samples."""
from __future__ import annotations

import io
from datetime import date, datetime

# Excel cells carry no zone: naive datetimes are what openpyxl reads.
# ruff: noqa: DTZ001
from openpyxl import Workbook, load_workbook

from audit_core import uc03_p2_dms_dumps as m

BOOKING_HEADERS = ["Dealer Name", "Dealer Location", "Sales Consultant", "SC MIle Id", "Sales Manager",
                   "Enquiry Number", "Booking Number", "DMS Booking Number", "Booking Stage", "Created Date",
                   "Payment Status", "Booking Amount", "Customer Name", "Customer Contact Address",
                   "Customer Mobile Phone", "Customer Tehsil", "Customer District", "Customer MailingPostalCode",
                   "Product Family", "Variant Description"]


def _book(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_the_dms_booking_report_is_read_by_its_columns() -> None:
    data = _book({"FETB_EDP_v1_1": [BOOKING_HEADERS, [
        "ADITYA MOTORS", "CUTTACK", "A SC", "X", "A SM", "EN-1", "B-13455387", "OTF27A090950", "Booked",
        "02-Nov-2026 07:25 PM", datetime(2026, 11, 2), 21000, "TEST CUSTOMER", "addr", "9000000001", "t", "d",
        "753001", "XEV 9S", "THREE"], [None] * 20]})
    parsed = m.parse_workbook(data)
    assert parsed.kind == m.BOOKING
    row = parsed.rows[0]
    assert len(parsed.rows) == 1
    assert row["booking_number"] == "B-13455387" and row["dms_booking_number"] == "OTF27A090950"
    assert row["event_date"] == date(2026, 11, 2) and row["mobile"] == "9000000001"
    assert row["outlet_label"] == "CUTTACK" and row["model"] == "XEV 9S"


def test_aditya_delivery_takes_the_first_date_and_the_chassis() -> None:
    headers = ["slno", "Dealer Name", "Date", "Chassis no", "Customer Name", "Model", "SC name", "Branch",
               "Invoice no", "Date", "Varient", "Color", "Phone No", "Booking No.", "Booking Date and Time"]
    data = _book({"Sheet1": [headers, [1, "Aditya Mahindra", datetime(2026, 11, 1), "T6K14763", "TEST ONE",
                                       "MAXX CITY", "SC", "CUTTACK", "INV27A003473", datetime(2026, 10, 28),
                                       "1.5 LX", "White", 9000000002, "B-13421434", datetime(2026, 10, 28)],
                             [None] * 15]})
    parsed = m.parse_workbook(data)
    assert parsed.kind == m.DELIVERY and parsed.file_format == "DELIVERY_REPORT_INVOICED"
    row = parsed.rows[0]
    assert row["event_date"] == date(2026, 11, 1)
    assert row["chassis"] == "T6K14763" and row["mobile"] == "9000000002" and row["booking_number"] == "B-13421434"


def test_shivnath_delivery_finds_the_phone_under_a_second_name_column_and_the_outlet() -> None:
    headers = ["DATE OF DELIVERY", "Location", "CX NAME", "CX NAME", "SEGMENT", "LOCATION", "MODEL", "VARIENT",
               "COLOUR", "VIN", "REGD.NO", "CONSULTANT"]
    rows = [[datetime(2026, 11, 1), "Shivnath Mahindra", f"Person {n}", str(7000000000 + n), "PV",
             "PURIBYPASS" if n % 2 else "TAMANDO", "XUV3XO", "AX5", "White", f"T2H7633{n}", "OD02", "SC"]
            for n in range(4)]
    parsed = m.parse_workbook(_book({"Sheet1": [headers, *rows]}))
    row = parsed.rows[1]
    assert row["customer_name"] == "Person 1" and row["mobile"] == "7000000001"
    assert row["outlet_label"] == "PURIBYPASS" and row["chassis"] == "T2H76331"


def test_utkal_form_responses_are_read_from_their_own_sheet() -> None:
    headers = ["Timestamp", "Dealer Name", "Location", "\nCUSTOMER NAME ", "\nCONT NO ", "\nSALES  MANAGER ",
               "\nFSC ", "BOOKING DATE", "\nDELIVERY DATE ", "\nDocket Sign", "\nMODEL ", "\nChassis No. "]
    data = _book({"Sheet1": [[None, None], ["Row Labels", "Count"], ["CDA", 7]],
                  "Form Responses 1": [headers, [datetime(2026, 11, 1, 18, 33), "Utkal Mahindra", "CDA",
                                                 "M/s. Test ", 9379327299, "SM ", "FSC ", None,
                                                 datetime(2026, 11, 1), None, "Bolero Neo N10", "T6J57000"]]})
    parsed = m.parse_workbook(data)
    assert parsed.sheet_name == "Form Responses 1" and parsed.file_format == "DELIVERY_FORM_RESPONSES"
    row = parsed.rows[0]
    assert row["event_date"] == date(2026, 11, 1) and row["chassis"] == "T6J57000" and row["outlet_label"] == "CDA"


def test_of_two_columns_of_one_name_the_one_with_values_is_read() -> None:
    """Utkal's delivery sheet of 19/20 Nov 2026 heads two columns BOOKING DATE, the first empty:
    the empty one was read and August bookings were marked Inscope on the VIN alone."""
    headers = ["Timestamp", "Location", "CUSTOMER NAME", "CONT NO", "BOOKING DATE", "MODEL", "CHASSIS NO",
               "BOOKING DATE", "DELIVERY DATE", "FSC"]
    data = _book({"Form Responses 1": [headers, [datetime(2026, 11, 10, 14, 3), "CDA", "Ayvens India Pvt Ltd",
                                                 9437033732, None, "XUV 3XO", "T2K55617", datetime(2026, 8, 11),
                                                 datetime(2026, 11, 6), "Swati Sangita Das"]]})
    row = m.parse_workbook(data).rows[0]
    assert row["booking_date"] == date(2026, 8, 11) and row["event_date"] == date(2026, 11, 6)


def test_a_date_kept_as_iso_text_is_read() -> None:
    assert m.parse_date("2026-08-11T00:00:00") == date(2026, 8, 11)
    assert m.parse_date("2026-11-10T14:03:33.568000") == date(2026, 11, 10)
    assert m.parse_date("not a date") is None


def test_booking_keys_meet_the_booking_forms_numbers() -> None:
    keys = m.booking_keys("B-13455387", "OTF27C022672")
    assert m.journey_ref_keys("B-13455387") & keys  # Aditya prints the B-number
    assert m.journey_ref_keys("13455387") & keys  # Shivnath its digits
    assert m.journey_ref_keys("27C022672") & keys  # Utkal the OTF without OTF
    assert m.journey_ref_keys("OTF NO-22672") & keys  # or its number
    assert not m.journey_ref_keys("116")  # a short number is no key


def test_rows_match_on_number_then_chassis_then_mobile() -> None:
    index = m.JourneyIndex()
    index.journeys = {
        "j1": {"journeyId": "j1", "model": "XUV 3XO", "names": {"Test One"}, "hasDelivery": True, "outletId": None},
        "j2": {"journeyId": "j2", "model": "THAR ROXX", "names": {"Other Person"}, "hasDelivery": False,
               "outletId": None},
    }
    for k in m.journey_ref_keys("B-13455387"):
        index.by_ref[k].add("j1")
    index.by_chassis[m.chassis_key("MA1NS2NP1T2K61232")].add("j2")
    index.by_mobile["9000000003"].add("j1")
    assert m.match_row(index, m.BOOKING, {"booking_number": "B-13455387"}) == ("j1", "BOOKING_NUMBER")
    jid, basis = m.match_row(index, m.DELIVERY, {"chassis": "T2K61232"})
    assert (jid, basis) == ("j2", "CHASSIS")
    assert m.outcome_of(m.DELIVERY, {}, index.journeys[jid]) == m.NOT_IN_TOOL
    assert m.match_row(index, m.DELIVERY, {"chassis": "X", "mobile": "9000000003"}) == ("j1", "MOBILE")
    assert m.match_row(index, m.BOOKING, {"customer_name": "Nobody", "model": "XEV"}) == (None, None)
    assert m.outcome_of(m.BOOKING, {"stage": "Cancellation"}, None) == m.CANCELLED


def test_a_delivery_row_goes_by_its_chassis_before_a_misfiled_booking_form() -> None:
    # 21 Nov, Aditya: customer B's booking form was filed on customer A's Journey too, so A's
    # Journey carried B's booking number. B's delivery row took A's Journey by that number and
    # A's own row (chassis on A's gate pass) was left "Not in Verigence".
    index = m.JourneyIndex()
    index.journeys = {
        "a": {"journeyId": "a", "model": "BOLERO NEO", "names": {"Customer A"}, "hasDelivery": True, "outletId": None},
        "b": {"journeyId": "b", "model": "XUV 700", "names": {"Customer B"}, "hasDelivery": True, "outletId": None},
    }
    for k in m.journey_ref_keys("B-00000002"):  # B's form, misfiled on A
        index.by_ref[k].add("a")
    index.by_chassis[m.chassis_key("T0A00001")].add("a")
    index.by_chassis[m.chassis_key("T0A00002")].add("b")
    row_b = {"booking_number": "B-00000002", "chassis": "T0A00002", "customer_name": "Customer B"}
    row_a = {"booking_number": "B-00000001", "chassis": "T0A00001", "customer_name": "Customer A"}
    assert m.match_row(index, m.DELIVERY, row_b) == ("b", "CHASSIS")
    assert m.match_row(index, m.DELIVERY, row_a, {"b"}) == ("a", "CHASSIS")
    # No chassis on the row: the booking number still finds it.
    assert m.match_row(index, m.DELIVERY, {"booking_number": "B-00000002"}) == ("a", "BOOKING_NUMBER")


def test_dump_locations_place_rows_on_outlets() -> None:
    outlets = [{"outlet_id": "a", "outlet_name": "Aditya Motors - Khordha"},
               {"outlet_id": "b", "outlet_name": "Aditya Motors - Bhubaneswar_Cube"},
               {"outlet_id": "c", "outlet_name": "Shivnath Motors - Kesora / Puri Bypass"}]
    assert m.outlet_for("KHORDHA_SZS", outlets) == "a"
    assert m.outlet_for("BHUBANESHW", outlets) == "b"
    assert m.outlet_for("PURI_BY_PASS_SH", outlets) == "c"
    assert m.outlet_for("CSA_SEC_10_SH", outlets) is None


# --------------------------------------------------------------------------- with the database

import json
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_read_value,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="dms")
    yield created
    delete_tenant_data(engine, created.tenant_id)


def _store(journey, kind: str, rows: list[dict]) -> str:
    dump_id = str(uuid4())
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("""
            INSERT INTO auditcore.p2_dms_dumps (tenant_id, dump_id, dump_kind, dealer_id, business_date, period_month,
              file_name, file_format, row_count, uploaded_by)
            VALUES (:t, :id, :k, :d, CURRENT_DATE, date_trunc('month', CURRENT_DATE - 1)::date, 'f.xlsx', 'X', :n, 'tl')
        """), {"t": journey.tenant_id, "id": dump_id, "k": kind, "d": str(journey.dealer_id), "n": len(rows)})
        for n, row in enumerate(rows, start=1):
            connection.execute(text("""
                INSERT INTO auditcore.p2_dms_dump_rows (tenant_id, dump_id, row_no, dump_kind, dealer_id, booking_number,
                  dms_booking_number, customer_name, mobile, chassis, model, stage, event_date, raw)
                VALUES (:t, :id, :n, :k, :d, :b, :o, :c, :m, :ch, :model, :stage,
                        CURRENT_DATE - CAST(:back AS integer), CAST(:raw AS jsonb))
            """), {"t": journey.tenant_id, "id": dump_id, "n": n, "k": kind, "d": str(journey.dealer_id),
                   "b": row.get("booking_number"), "o": row.get("dms_booking_number"), "c": row.get("customer_name"),
                   "m": row.get("mobile"), "ch": row.get("chassis"), "model": row.get("model"), "stage": row.get("stage"),
                   # the day before the upload (compared), unless the row says otherwise
                   "back": row.get("days_back", 1),
                   "raw": json.dumps({})})
    return dump_id


def test_a_dump_is_matched_marked_reported_and_shown_on_the_calendar(journey) -> None:
    add_read_value(journey, field_key="booking_reference_number", value="B-13455387")
    # The chassis known from a paper that is not a delivery paper (the RC): found by the delivery
    # dump, but no delivery file on the journey (an invoice printing it would be one).
    add_read_value(journey, field_key="vin_number", value="MA1NS2NP1T2K61232", document_type="vehicle_rc")
    booking = _store(journey, m.BOOKING, [
        {"booking_number": "B-13455387", "dms_booking_number": "OTF27A090950", "customer_name": "P2 Customer"},
        {"booking_number": "B-99999999", "dms_booking_number": "OTF27A000001", "customer_name": "Someone Else"},
        {"booking_number": "B-88888888", "customer_name": "Gone", "stage": "Cancellation"},
        # dated after the upload day: outside the days compared, not counted (2026-11-18)
        {"booking_number": "B-77777777", "customer_name": "Tomorrow Only", "days_back": -1},
    ])
    delivery = _store(journey, m.DELIVERY, [{"chassis": "T2K61232", "customer_name": "P2 Customer"}])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert m.match_dump(connection, tenant_id=journey.tenant_id, dump_id=booking) == {
            m.MATCHED: 1, m.NOT_IN_TOOL: 0, m.MISSING: 1, m.CANCELLED: 1}
        # Found, but no delivery file on the journey yet.
        assert m.match_dump(connection, tenant_id=journey.tenant_id, dump_id=delivery)[m.NOT_IN_TOOL] == 1
        # Checking again changes nothing and keeps the first sync time.
        first = connection.execute(text("SELECT dms_booking_synced_at FROM auditcore.p2_journey_dms_sync "
                                        "WHERE journey_id = :j"), {"j": journey.journey_id}).scalar_one()
        m.match_dump(connection, tenant_id=journey.tenant_id, dump_id=booking)
        sync = connection.execute(text("SELECT * FROM auditcore.p2_journey_dms_sync WHERE journey_id = :j"),
                                  {"j": journey.journey_id}).mappings().one()
        assert sync["dms_booking_synced_at"] == first and sync["dms_booking_dump_id"] is not None
        assert sync["dms_delivery_synced_at"] is not None
        today_db = connection.execute(text("SELECT CURRENT_DATE")).scalar_one()
        data = m.build_report(connection, tenant_id=journey.tenant_id, dealer_ids=[str(journey.dealer_id)],
                              first=today_db.replace(day=1))
        book = load_workbook(io.BytesIO(data))
        assert book.sheetnames[:3] == ["Summary", "Booking dump variance", "Delivery dump variance"]
        missing = [r for r in book["Booking dump variance"].iter_rows(values_only=True)][1:]
        assert [r[8] for r in missing] == ["Someone Else"]  # the cancelled booking is not missing
        assert [r[4] for r in book["Delivery dump variance"].iter_rows(values_only=True)][1:] == [
            "Booking on file, delivery not uploaded"]
        # One day's uploads only (2026-11-18): a day with no upload gives an empty report.
        today_dumps = m.build_report(connection, tenant_id=journey.tenant_id, dealer_ids=[str(journey.dealer_id)],
                                     first=today_db.replace(day=1), day=today_db)
        assert load_workbook(io.BytesIO(today_dumps))["Summary"].max_row > 1
        other_day = today_db.replace(day=1) if today_db.day != 1 else today_db.replace(day=2)
        none = m.build_report(connection, tenant_id=journey.tenant_id, dealer_ids=[str(journey.dealer_id)],
                              first=today_db.replace(day=1), day=other_day)
        assert load_workbook(io.BytesIO(none))["Summary"].max_row == 1
        calendar = m.dump_calendar(connection, tenant_id=journey.tenant_id, dealer_ids=[str(journey.dealer_id)],
                                   first=today_db.replace(day=1), today=today_db)
        today = next(d for d in calendar["dealers"][0]["days"] if d["date"] == today_db.isoformat())
        assert today["booking"]["status"] == "UPLOADED" and today["booking"]["missing"] == 1


def test_a_team_lead_s_bundle_is_enough_to_upload() -> None:
    """A Team Lead's bundle has audit.work.update and no audit.journey.update: asking for
    journey.update alone refused every TL upload (2026-11-14, VAC-AUTH-002)."""
    assert "audit.work.update" in m._UPDATE and "audit.journey.update" in m._UPDATE
    assert set(m.UPLOAD_ROLES) == {"TL", "REVIEWER"}


def test_a_model_spaced_or_extended_differently_is_the_same_car() -> None:
    # 2026-11-18: Shivnath's bookings on file were reported "not in Verigence" because the
    # model names were spaced differently in the DMS and on the papers.
    assert m.same_model("XEV9S", "XEV 9S")
    assert m.same_model("BE6 sportber threet 79 NCH", "BE 6")
    assert m.same_model("3XO", "XUV3XO EV")
    assert not m.same_model("THAR ROXX", "XUV 3XO")
    assert not m.same_model("BE", "BE 6")  # too short to stand for a model
    index = m.JourneyIndex()
    index.journeys = {"j1": {"journeyId": "j1", "model": "XEV9S", "names": {"RENUBALA ROUT"}, "hasDelivery": False,
                             "outletId": None}}
    assert m.match_row(index, m.BOOKING, {"customer_name": "RENUBALA ROUT", "model": "XEV 9S"}) == ("j1", "NAME_MODEL")


def test_a_booking_row_matches_on_the_name_and_the_booking_date() -> None:
    # 2026-11-18: no OTF and no mobile on the papers; the DMS booking date tells two
    # customers of one name apart, whatever the model is called.
    from datetime import date

    index = m.JourneyIndex()
    index.journeys = {
        "j1": {"journeyId": "j1", "model": "XEV9S", "names": {"RENUBALA ROUT"}, "hasDelivery": False,
               "outletId": None, "bookingDays": {date(2026, 11, 7)}},
        "j2": {"journeyId": "j2", "model": "THAR", "names": {"Renubala Rout"}, "hasDelivery": False,
               "outletId": None, "bookingDays": {date(2026, 9, 1)}},
    }
    row = {"customer_name": "RENUBALA ROUT", "model": "Something else", "event_date": date(2026, 11, 8)}
    assert m.match_row(index, m.BOOKING, row) == ("j1", "NAME_DATE")
    # a month away from both, a different model: no Journey is taken on the name alone
    assert m.match_row(index, m.BOOKING, {**row, "event_date": date(2026, 10, 10)}) == (None, None)


def test_the_name_and_a_mobile_or_booking_number_a_digit_or_two_off_find_the_customer() -> None:
    """2026-11-21, Aditya's Sujit Kumar Bastia: the booking form read 9337354361 for 9337359361,
    B-1348698 for B-13486428, its date as 2020 and the model XUV-9S for XEV 9S."""
    from datetime import date

    def journey(jid: str, name: str, mobiles: set[str], refs: set[str]) -> dict:
        return {"journeyId": jid, "model": "XUV-9S", "names": {name}, "hasDelivery": False, "outletId": None,
                "bookingDate": None, "bookingDays": {date(2026, 11, 20)}, "mobiles": mobiles, "refs": refs}

    index = m.JourneyIndex()
    index.journeys = {"j1": journey("j1", "Sujit Kumar Bastia", {"9337354361"}, {"1348698"}),
                      "j2": journey("j2", "Anita Sahoo", {"9337359361"}, set())}
    row = {"customer_name": "Sujit Kumar bastia", "mobile": "9337359361", "booking_number": "B-13486428",
           "dms_booking_number": "OTF27A092698", "model": "XEV 9S", "event_date": date(2026, 11, 7)}
    # j2 holds the DMS mobile but is another customer: the mobile alone takes nothing
    # (2026-11-24), so the booking number a digit or two off with the name finds j1.
    index.by_mobile["9337359361"] = {"j2"}
    assert m.match_row(index, m.BOOKING, row) == ("j1", "NEAR_BOOKING_NUMBER")
    assert m.match_row(index, m.BOOKING, {**row, "mobile": "7000000000"}) == ("j1", "NEAR_BOOKING_NUMBER")
    # the mobile alone a digit or two off, with the name
    del index.journeys["j2"]
    index.by_mobile.clear()
    assert m.match_row(index, m.BOOKING, {**row, "booking_number": "B-13999999", "dms_booking_number": None}) \
        == ("j1", "NAME_NEAR_ID")
    # the same name, a mobile and booking number far off: another customer, not taken
    assert m.match_row(index, m.BOOKING, {**row, "mobile": "7000000000", "booking_number": "B-13999999",
                                          "dms_booking_number": None}) == (None, None)
    # two Journeys of that name: the one whose booking number is a digit or two off is it
    # (2026-11-24); both only a mobile a digit or two off: not decided on the name
    index.journeys["j3"] = journey("j3", "SUJIT KUMAR BASTIA", {"9337359362"}, set())
    assert m.match_row(index, m.BOOKING, row) == ("j1", "NEAR_BOOKING_NUMBER")
    assert m.match_row(index, m.BOOKING, {**row, "booking_number": None, "dms_booking_number": None}) == (None, None)
    assert m.near_mobile("9337359361", {"9337354361"}) and not m.near_mobile("9337359361", {"9437000361"})
    assert m.near_booking({"13486428"}, {"1348698"}) and not m.near_booking({"21913"}, {"21914"})


def test_name_and_date_use_the_booking_form_or_the_first_receipt_within_two_days() -> None:
    """The booking date (product owner 2026-11-22): the booking form's date or the first money
    receipt's, within two days of the DMS's -- never the upload day (the index carries none)."""
    from datetime import date

    index = m.JourneyIndex()
    index.journeys = {"j1": {"journeyId": "j1", "model": "THAR", "names": {"Pankaj Dutta"}, "hasDelivery": False,
                             "outletId": None, "bookingDate": None,
                             # no booking form date read; the first money receipt of the 10th
                             "bookingDays": {date(2026, 11, 10)}, "mobiles": set(), "refs": set()}}
    row = {"customer_name": "PANKAJ DUTTA", "model": "XUV 7XO", "event_date": date(2026, 11, 8)}
    assert m.match_row(index, m.BOOKING, row) == ("j1", "NAME_DATE")              # two days off
    assert m.match_row(index, m.BOOKING, {**row, "event_date": date(2026, 11, 7)}) == (None, None)  # three
    # the booking form's date or the receipt's: either within two days
    index.journeys["j1"]["bookingDays"] = {date(2026, 11, 1), date(2026, 11, 10)}
    assert m.match_row(index, m.BOOKING, {**row, "event_date": date(2026, 11, 2)}) == ("j1", "NAME_DATE")
    # nothing read: no date to match on
    index.journeys["j1"]["bookingDays"] = set()
    assert m.match_row(index, m.BOOKING, row) == (None, None)


def test_a_dump_is_compared_from_the_first_to_its_upload_day() -> None:
    """2026-11-18: uploaded on the 18th, compared for the 1st to the 18th."""
    from datetime import date

    assert m.dump_window(date(2026, 11, 1), date(2026, 11, 18)) == (date(2026, 11, 1), date(2026, 11, 18))
    # uploaded on 1 Dec with November's rows: the whole of November
    assert m.dump_window(date(2026, 11, 1), date(2026, 12, 1)) == (date(2026, 11, 1), date(2026, 11, 30))


def test_a_dump_is_compared_up_to_the_last_day_its_rows_reach(journey) -> None:
    """2026-11-22: a dump uploaded on the 21st whose rows stop at the 19th holds nothing of the
    20th -- the bookings of the 20th are not compared with it."""
    from datetime import timedelta

    dump_id = _store(journey, m.BOOKING, [{"booking_number": "B-1", "customer_name": "A", "days_back": 3},
                                         {"booking_number": "B-2", "customer_name": "B", "days_back": 5}])
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        dump = dict(connection.execute(text(
            "SELECT dump_id, business_date, period_month FROM auditcore.p2_dms_dumps WHERE dump_id = :i"),
            {"i": dump_id}).mappings().one())
        window_end = m.dump_window(dump["period_month"], dump["business_date"])[1]
        assert m.compared_until(connection, journey.tenant_id, dump) == min(
            window_end, dump["business_date"] - timedelta(days=3))


# --------------------------------------------------------------------------- 24 Nov 2026: sheet checks
# Utkal's "ALL BRANCH DELIVERY SHEET 24.11.26.xlsx" came with its LOCATION column headed
# "Delivery Date": all 348 rows were taken with no date and no outlet.

UTKAL = ["Timestamp", "Location", "SALES MANAGER", "FSC", "CUSTOMER NAME", "CONT NO", "MODEL", "CHASSIS NO",
         "BOOKING DATE", "DELIVERY DATE"]


def _utkal(headers: list[str], rows: list[list]) -> bytes:
    return _book({"Form Responses 1": [headers, *rows]})


def _line(location: object = "CDA", booked: object = datetime(2026, 10, 23), delivered: object = datetime(2026, 11, 6),
          chassis: str = "T6K14763", name: str = "Tridev Associates") -> list:
    return [datetime(2026, 11, 7, 9, 51), location, "SM", "FSC", name, 9437033732, "XUV 3XO", chassis, booked, delivered]


def _refused(data: bytes) -> str:
    try:
        m.parse_workbook(data)
    except m.HTTPException as exc:
        assert exc.status_code == 422
        return str(exc.detail)
    raise AssertionError("the sheet was taken")


def test_a_location_column_headed_like_the_date_is_refused_naming_both_columns() -> None:
    headers = ["Timestamp", "Delivery Date", *UTKAL[2:]]  # LOCATION's heading typed over as "Delivery Date"
    detail = _refused(_utkal(headers, [_line("CDA", chassis=f"T6K1476{i}") for i in range(4)]))
    assert "'Delivery Date' column (column B) has no readable delivery date in 4 of 4 rows" in detail
    assert "'CDA'" in detail
    assert "Two columns are headed 'Delivery Date' (columns B and J)" in detail and "Location" in detail
    assert "No location column was found" in detail


def test_a_sheet_without_its_location_column_is_refused() -> None:
    headers = [h for h in UTKAL if h != "Location"]
    rows = [[v for k, v in enumerate(_line()) if k != 1]]
    assert "No location column was found ('Branch' / 'Location' / 'Dealer Location')" in _refused(_utkal(headers, rows))


def test_a_sheet_with_most_dates_unreadable_or_locations_empty_is_refused() -> None:
    rows = [_line(delivered="pending", chassis=f"T6K1476{i}") for i in range(3)] + [_line(chassis="T6K14769")]
    assert "no readable delivery date in 3 of 4 rows" in _refused(_utkal(UTKAL, rows))
    rows = [_line(location=None, chassis=f"T6K1476{i}") for i in range(3)] + [_line(chassis="T6K14769")]
    assert "'Location' column (column B) is empty in 3 of 4 rows" in _refused(_utkal(UTKAL, rows))


def test_wrong_dates_in_a_good_sheet_are_left_empty_and_listed_never_guessed() -> None:
    rows = [_line(chassis="T6K14760", booked="9/30/0026", name="RENU DUNGDUNG"),
            _line(chassis="T6K14761", delivered="11/3/0026", name="Asha Das"),
            _line(chassis="T6K14762", booked=datetime(2026, 11, 23), delivered=datetime(2026, 11, 1),
                  name="M/s.Tridev Associates"),
            _line(chassis="T6K14763"), _line(chassis="T6K14764")]
    parsed = m.parse_workbook(_utkal(UTKAL, rows))
    by_chassis = {r["chassis"]: r for r in parsed.rows}
    assert by_chassis["T6K14760"]["booking_date"] is None and by_chassis["T6K14760"]["event_date"] == date(2026, 11, 6)
    assert by_chassis["T6K14761"]["event_date"] is None  # not 11 March of the year 26
    assert by_chassis["T6K14763"]["event_date"] == date(2026, 11, 6)
    assert parsed.row_issues == [
        {"sheetRow": 2, "customer": "RENU DUNGDUNG", "chassis": "T6K14760", "field": "BOOKING DATE",
         "value": "9/30/0026", "issue": "is not a date"},
        {"sheetRow": 3, "customer": "Asha Das", "chassis": "T6K14761", "field": "DELIVERY DATE",
         "value": "11/3/0026", "issue": "has the year 0026"},
        {"sheetRow": 4, "customer": "M/s.Tridev Associates", "chassis": "T6K14762", "field": "BOOKING DATE",
         "value": "23 Nov 2026", "issue": "is after the DELIVERY DATE 01 Nov 2026"},
    ]


def test_the_sheet_date_reads_a_date_and_says_what_is_wrong() -> None:
    assert m.sheet_date(datetime(2026, 11, 6)) == (date(2026, 11, 6), None)
    assert m.sheet_date("06-11-2026") == (date(2026, 11, 6), None)
    assert m.sheet_date(None) == (None, None) and m.sheet_date("") == (None, None)
    assert m.sheet_date("pending") == (None, "is not a date")
    assert m.sheet_date(datetime(26, 3, 11)) == (None, "has the year 0026")


def test_excel_column_letters() -> None:
    assert [m._letter(i) for i in (0, 1, 25, 26, 27, 51, 52)] == ["A", "B", "Z", "AA", "AB", "AZ", "BA"]


# --------------------------------------------------------------------------- 24 Nov 2026: booking ladder

def _ladder_index(*journeys: dict) -> m.JourneyIndex:
    index = m.JourneyIndex()
    for j in journeys:
        index.journeys[j["journeyId"]] = j
        for mobile in j["mobiles"]:
            index.by_mobile[mobile].add(j["journeyId"])
        for ref in j["refs"]:
            index.by_ref[ref].add(j["journeyId"])
    return index


def _j(jid: str, name: str, model: str, booked: date | None, mobiles: set[str] = frozenset(),
       refs: set[str] = frozenset()) -> dict:
    return {"journeyId": jid, "model": model, "names": {name}, "hasDelivery": False, "outletId": None,
            "bookingDate": booked, "bookingDays": {booked} if booked else set(), "mobiles": set(mobiles),
            "refs": set(refs), "reference": jid}


def test_the_mobile_alone_or_the_name_alone_matches_nothing() -> None:
    # Prajnaraj Pradhan's DMS booking took MANORAMA CONSTRUCTIONS' Journey on a mobile.
    index = _ladder_index(_j("j1", "MANORAMA CONSTRUCTIONS", "SCORPIO N", date(2026, 10, 13), {"9437000001"}))
    row = {"customer_name": "Prajnaraj Pradhan", "mobile": "9437000001", "model": "THAR",
           "booking_number": "B-13478700", "event_date": date(2026, 11, 6)}
    assert m.match_row(index, m.BOOKING, row) == (None, None)
    # the mobile and the booking date (within a week) are two things: a company and its owner
    assert m.match_row(index, m.BOOKING, {**row, "event_date": date(2026, 10, 18)}) == ("j1", "MOBILE_DATE")
    # the mobile and the name
    assert m.match_row(index, m.BOOKING, {**row, "customer_name": "Manorama Constructions"}) == ("j1", "MOBILE_NAME")


def test_a_rebooking_does_not_take_the_cancelled_bookings_journey() -> None:
    """N.Santosh Kumar Choudhury: OTF27C053725 of 14 Nov (cancelled in the DMS) and his 19 Nov
    rebooking OTF27C054617, one mobile; the Journey (its OTF read OTF2705372) is the 14 Nov one."""
    index = _ladder_index(_j("j1", "NARAYANSINTI SANTOSH KUMAR CHHOUDHURY", "XUV3XO", date(2026, 11, 14),
                             {"6371500265"}, m.journey_ref_keys("OTF2705372")))
    new = {"customer_name": "N.Santosh Kumar Choudhury", "mobile": "6371500265", "model": "XUV3XO",
           "booking_number": "B-13536610", "dms_booking_number": "OTF27C054617", "event_date": date(2026, 11, 19)}
    old = {"customer_name": "NARAYANSINTI SONTOSH KUMAR CHOUDHARY", "mobile": "6371500265", "model": "XUV3XO",
           "booking_number": "B-13512229", "dms_booking_number": "OTF27C053725", "event_date": date(2026, 11, 14),
           "stage": "Cancelled"}
    # each alone would take the Journey on the mobile and the name ...
    assert m.match_row(index, m.BOOKING, new) == ("j1", "MOBILE_NAME")
    assert m.match_row(index, m.BOOKING, old) == ("j1", "MOBILE_NAME")
    # ... the one booked nearest the Journey's date claims it first, cancelled or not
    assert m.days_apart(index, "j1", old["event_date"]) == 0 and m.days_apart(index, "j1", new["event_date"]) == 5
    assert m.match_row(index, m.BOOKING, new, {"j1"}) == (None, None)


def test_a_name_and_model_match_needs_the_name_word_for_word_and_a_booking_within_45_days() -> None:
    """Chitta Ranjan Patra's 19 Nov booking took RANJANA PATRA's (12 Sep) on a name and a model."""
    index = _ladder_index(_j("j1", "RANJANA PATRA", "XUV 7XO", date(2026, 9, 12)))
    row = {"customer_name": "Chitta Ranjan Patra", "mobile": "8847871001", "model": "XUV 7XO",
           "booking_number": "B-13537274", "dms_booking_number": "OTF27C054649", "event_date": date(2026, 11, 19)}
    assert m.match_row(index, m.BOOKING, row) == (None, None)
    # the same name word for word, two months apart: still not
    assert m.match_row(index, m.BOOKING, {**row, "customer_name": "Ranjana Patra"}) == (None, None)
    # word for word and within 45 days (Menaka Behera: 31 days): matched
    index = _ladder_index(_j("j2", "MENAKA BEHERA", "BOLERO NEO", date(2026, 10, 11)))
    menaka = {"customer_name": "MENAKA BEHERA", "model": "BOLERO NEO", "booking_number": "B-13495813",
              "event_date": date(2026, 11, 11)}
    assert m.match_row(index, m.BOOKING, menaka) == ("j2", "NAME_MODEL")
    # a name the tolerant check takes with a word missing is not word for word
    index = _ladder_index(_j("j3", "PRAKASH KUMAR DASH", "XUV 3XO", date(2026, 10, 20)))
    assert m.match_row(index, m.BOOKING, {"customer_name": "PRAKASH DAS", "model": "XUV 3XO",
                                          "event_date": date(2026, 11, 13)}) == (None, None)


def test_the_otf_a_digit_or_two_off_with_the_name_comes_before_the_mobile() -> None:
    index = _ladder_index(
        _j("j1", "SOMANATH MAHANTA", "XUV 3XO", date(2026, 10, 30), refs=m.journey_ref_keys("B-13339384")),
        _j("j2", "SOMANATH MAHANTA", "XUV 3XO", date(2026, 10, 2), {"9000000001"}))
    row = {"customer_name": "Somanath Mahanta", "mobile": "9000000001", "model": "XUV 3XO",
           "booking_number": "B-13539384", "event_date": date(2026, 11, 20)}
    assert m.match_row(index, m.BOOKING, row) == ("j1", "NEAR_BOOKING_NUMBER")


def test_the_delivery_ladder_is_unchanged() -> None:
    # a delivery row still matches on the mobile alone (insurance, scheme dumps rely on it)
    index = _ladder_index(_j("j1", "ASHA DAS", "THAR", date(2026, 10, 1), {"9437000009"}))
    assert m.match_row(index, m.DELIVERY, {"customer_name": "Someone Else", "mobile": "9437000009",
                                           "model": "THAR"}) == ("j1", "MOBILE")


def test_two_customers_on_one_journeys_papers_are_not_decided_on_the_name() -> None:
    """PRASANTA KUMAR SAHU and BHARAT NAIK, both booked 17 Nov: Bharat's Journey also carries
    Prasanta's name. Prasanta's row is not decided between the two on the name; once Bharat's
    row has his Journey, Prasanta's finds its own."""
    index = _ladder_index(_j("j2", "BHARAT NAIK", "BOLERO", date(2026, 11, 16)),
                          _j("j3", "PRASANTA KUMAR SAHU", "BOLERO", date(2026, 11, 16)))
    index.journeys["j2"]["names"].add("Prasanta Kumar Sahu")
    prasanta = {"customer_name": "PRASANTA KUMAR SAHU", "model": "BOLERO", "event_date": date(2026, 11, 17)}
    bharat = {"customer_name": "BHARAT NAIK", "model": "BOLERO", "event_date": date(2026, 11, 17)}
    assert m.match_row(index, m.BOOKING, prasanta) == (None, None)
    assert m.match_row(index, m.BOOKING, bharat) == ("j2", "NAME_DATE")
    assert m.match_row(index, m.BOOKING, prasanta, {"j2"}) == ("j3", "NAME_DATE")
